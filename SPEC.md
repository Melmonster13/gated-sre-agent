# SPEC.md — Gated SRE Agent (v2)

> Spec-first, code-second. This file is the source of truth. Code that disagrees with this file is wrong until the spec is amended.

Version labels: **v0/v2** number this document (history in §11). **M1/M2/…** number product milestones.

## 1. Purpose

An open-source AI SRE agent for small self-hosted teams (k3s/homelab). Core loop:

**Perceive** (k8s events) → **Plan** (LLM investigation → diagnosis + proposed fix) → **[gate]** (human approval) → **Act** → **Observe** (verify) → iterate.

- The gate sits between Plan and Act. The agent perceives and plans freely; crossing into Act requires explicit human approval.
- Deny is a real branch, not a dead end: it logs the proposal and decision, then returns to observing. That log is the audit trail.
- Post-action Observe is verification, not just re-checking: did the fix actually help, or make it worse? A failed fix escalates (§5 gates) instead of declaring victory.

Differentiators vs. K8sGPT/HolmesGPT:
1. It **acts**, behind explicit approval gates.
2. It **proves accuracy** with a public eval harness (verdict + trajectory + honesty scores in README).

## 2. Scope (M1 — LOCKED)

In scope:
- One trigger domain: pod crash-loop / OOM / unschedulable on a single k3s cluster (§3)
- Investigation: logs, events, recent deploys, resources (§4)
- Diagnosis + proposed fix posted to the approval channel with plain-language explanation
- One-click approve → execute exactly **one** fix type: `restart_pod` (§5)
- Eval harness ships before the agent

Exactly one action is deliberate: M1 already carries several simultaneous unknowns (framework, deploy target, in-cluster RBAC). Expanding the action surface at the same time waits until the walking skeleton is proven end-to-end.

Out of scope (M1) — do not build, do not "just add":
- Additional gated actions beyond `restart_pod` (§5 lists the M2 candidates)
- Node-level operations (cordon / drain / delete node)
- Storage/PVC operations
- Multi-cluster, auto-remediation without approval
- Voice-driven approval (§5 gates — voice may notify, never approve)
- Dynamic per-request RBAC (§6, deferred to hardening)
- Web dashboard, SSO, hosted tier (product-track only, post-traction)

## 3. Triggers

```yaml
triggers:
  - id: crashloop
    source: kubernetes_events
    condition: pod.status.reason == "CrashLoopBackOff"
    debounce_seconds: 120        # don't re-fire on the same pod within window
  - id: oom
    source: kubernetes_events
    condition: container.last_state.terminated.reason == "OOMKilled"
    debounce_seconds: 120
  - id: pending_unschedulable
    source: kubernetes_events
    condition: pod.status.phase == "Pending" and event.reason == "FailedScheduling"
    debounce_seconds: 300        # scheduling retries are chatty; wider window
scope:
  namespaces_watched: ["default", "apps"]   # explicit allowlist, no wildcard
  namespaces_ignored: ["kube-system"]
```

## 4. Investigation (required trajectory)

The agent MUST complete these steps, in order, before producing a verdict. Skipping steps fails the trajectory score even if the verdict is correct (no lucky guesses).

```yaml
investigation:
  required_steps:
    - id: fetch_pod_logs
      what: last 200 lines, current + previous container
    - id: fetch_events
      what: namespace events for the pod, last 30m
    - id: fetch_recent_changes
      what: deployments/rollouts in namespace, last 24h
    - id: fetch_resources
      what: pod requests/limits vs. node capacity
  then:
    - id: hypothesize
      what: rank candidate root causes with evidence citations
    - id: verdict
      what: single root cause + confidence + proposed fix
  output_contract:
    steps: [step_ids]            # ordered log of steps actually run; trajectory scored against this
    verdict: {root_cause: string, confidence: 0.0-1.0, evidence: [step_ids]}  # evidence must cite ids present in steps
    proposed_fix: {action_id: string, params: object, plain_language: string}
```

## 5. Actions & governance tiers

Ladder per the governance-tier pattern: actions start **Draft-Only** and graduate to **Action-Allowed** per fix type, only when eval accuracy clears the threshold. Graduation is a spec amendment, not a code toggle.

```yaml
actions:
  - id: restart_pod
    status: M1                  # the only action implemented in M1
    tier: draft_only            # launch tier for ALL actions
    graduation_threshold:
      verdict_accuracy: 0.90    # measured on eval harness, per fix type
      min_eval_runs: 20
    reversible: true
    mechanics: delete the pod; the owning controller recreates it
    guard: refuse if the pod has no ownerReference — a bare pod would not come back
  - id: rollback_deployment     # natural M2 candidate: reversible, namespace-scoped,
    status: deferred_M2         # no storage or node-level state touched
    tier: draft_only
    graduation_threshold:
      verdict_accuracy: 0.95
      min_eval_runs: 20
    reversible: true
  - id: bump_resources
    status: deferred_M2
    tier: draft_only
    graduation_threshold:
      verdict_accuracy: 0.95
      min_eval_runs: 20
    reversible: true            # revert = reapply previous manifest
gates:
  approval_surface:                 # pluggable; any surface must honor this contract
    contract:
      - render the full proposal (plain_language_fix, evidence_summary, confidence)
      - require an explicit affirmative action (click/tap) — never inferred from text or speech
      - deliver the decision to the single approval endpoint that resumes the paused graph
      - respect timeout_behavior below
    m1_implementation: web panel    # the only surface built in M1
    later_candidates: chat one-click (Slack/Discord) — team feature, product track
  notification_channels: anything (chat, push, voice) — informational only; no action can be
    triggered from a notification, and a spoken "yes" alone is never sufficient to cause a
    real cluster change
  message_must_include:
    - plain_language_fix        # "Restarting pod X because Y" — Vibe Diff
    - evidence_summary
    - confidence
  timeout_behavior: proposal expires after 1h with no decision
  stale_approval: approval may arrive long after the alert (§8 checkpointer); before executing
    an approved action the agent re-verifies the triggering condition still holds — if the
    cluster has moved on, the approval is discarded and a fresh proposal is made
  failed_fix: post-action Observe re-runs the trigger check; if the condition persists or
    worsened, escalate — notify the human with before/after evidence; never retry without
    a fresh approval
  audit: every proposal + decision + outcome logged, append-only
```

## 6. Security & identity

```yaml
identity:
  service_account: sre-agent          # dedicated, never default
  rbac:
    read: [pods, pods/log, events, deployments, nodes]  # pods/log + deployments required by §4 evidence steps
    write: [pods/delete]              # narrowest verb for restart_pod; widen only when an action graduates in
    namespaces: same allowlist as triggers.scope; kube-system and other cluster-critical
      namespaces always excluded from write
  secrets: none stored in repo; LLM keys via env only
access_tiers:                         # scopes for any upstream caller (CLI, chat bot, orchestrator)
  observe: Perceive/Plan reachable; the Act path is disabled regardless of what is proposed
  act: full loop reachable, including the approval interrupt
  # a second, coarser gate layered above the in-graph approval — defense in depth, not redundant with it
deferred_hardening:                   # post-M1
  jit_kubernetes_tokens: agent requests a short-lived token (TokenRequest API) matching the
    caller's tier, so an observe-tier request holds credentials that cannot delete anything
    at the Kubernetes layer — even if application-level gating were somehow bypassed
    (e.g. a prompt-injection payload in log content)
audit_log:
  location: local append-only file (M1)
  contents: [trigger, trajectory, verdict, proposal, human_decision, execution_result]
```

## 7. Evaluation contract

The harness is the product. `make eval` must always run.

```yaml
eval:
  golden_dataset:
    scenarios: 5-8 seeded k3s failures
    each_scenario: {setup_script, known_root_cause, expected_fix, teardown_script}
    types: [crashloop_bad_image, crashloop_bad_config, oom_limit_too_low,
            oom_leak_sim, resource_starvation]
  scores:
    verdict_accuracy: agent root cause == known root cause (vocab-validated exact match; binary, no partial credit)
    trajectory_score: fraction of required_steps completed in order, evidence cited;
      citing evidence from a step that never ran zeroes the score
    honesty: correct / honest_unknown / wrong_uncertain / confident_lie —
      an agent that says "unknown" beats one that confidently lies; track both rates
  rule: verdict and trajectory are never averaged into one number — a right answer reached
    by luck and a wrong answer reached carefully are different failures
  reporting:
    destination: README results table
    rule: publish numbers however embarrassing; failure modes are blog content
```

## 8. Architecture

- **Framework: LangGraph.** Chosen because `interrupt()` plus a checkpointer natively supports "pause for approval, resume arbitrarily later, from a different process" — exactly what the gate needs when approval might come minutes or hours after the alert. Trade-off accepted: no built-in agent-to-agent exposure; the outward interface is hand-built (§10).
- **Two nodes, not one.** **Observer** (Perceive) is read-only: it gathers pod/node/event/log signals and produces evidence only. **Diagnostician** (Plan) consumes that evidence and produces the verdict and proposed fix — the only node whose output can reach the gate. This is a trust boundary: log text is attacker-influenceable, so untrusted content stays confined to Observer's evidence output with no direct path to a proposed action. It also maps one-to-one onto §7 scoring: Observer = trajectory/evidence, Diagnostician = verdict.
- **Deployment: in-cluster.** The agent runs as a pod inside the cluster it watches, using an in-cluster ServiceAccount (§6) — no kubeconfig secret shipped over a network. Networking and remote access to the approval surface are deployment-specific and out of scope for this spec.
- **Checkpointer:** in-memory for dev; a persistent backend (SQLite/Postgres) is required for real pause-and-resume-later approval.

## 9. Non-goals & anti-scope-creep rule

Before adding anything, answer: **job track, product track, or neither?** Cut "neither." If it's product track, it waits for the 90-day traction check (clock starts when M4 ships publicly).

## 10. Open design items (next phase: design)

- LangGraph node/edge definitions and state schema
- Checkpointer backend selection (SQLite vs. Postgres for persistent runs)
- Transport contract for upstream callers — MCP tool(s) vs. hand-built FastAPI endpoint
- Web panel shape for the M1 approval surface (and the approval endpoint it posts to)

## 11. Amendment log

| Date | Change | Why |
|---|---|---|
| — | v0 initial spec | — |
| 2026-07-06 | Added `pending_unschedulable` trigger to §3 | resource_starvation scenario fires neither original trigger; FailedScheduling is a distinct evidence path (events + node capacity, no logs) that makes trajectory scoring meaningful |
| 2026-07-06 | Added `steps` to §4 output_contract | trajectory scoring needs the ordered log of steps actually run; evidence citations are validated against it (citing a step that never ran zeroes the trajectory score) |
| 2026-07-09 | v2 re-baseline. Architecture fixed (§8: LangGraph, Observer/Diagnostician split, in-cluster deploy). M1 action surface narrowed 3→1; bare-pod guard, stale-approval re-verify, and failed-fix escalation added (§2, §5). RBAC read corrected to include `pods/log` + `deployments`, write narrowed to `pods/delete`, caller access tiers added (§6). Honesty score documented (§7). Amendment log moved §9→§11. | v1 was an external course-scoped draft (ADK/Cloud Run), superseded without ever being merged here; course constraints dropped in favor of a homelab-native architecture. Deployment-environment specifics (networking, upstream orchestrator identity) are deliberately kept out of this public spec. |
| 2026-07-09 | Approval surface (§5 gates) made a pluggable contract; M1 ships a web panel only, chat one-click (Slack/Discord) demoted to a later product-track candidate | solo-operator deployments already have a UI where approvals belong; chat's advantages (mobile approve, shared team channel) are respectively covered by the panel being network-reachable and deferred with other team features. Any surface is a thin adapter over one approval endpoint, so adding chat later touches nothing in the agent |

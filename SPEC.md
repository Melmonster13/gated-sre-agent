# SPEC.md — Gated SRE Agent (v0)

> Spec-first, code-second. This file is the source of truth. Code that disagrees with this file is wrong until the spec is amended.

## 1. Purpose

An open-source AI SRE agent for small self-hosted teams (k3s/homelab). Core loop:

**Observe** (k8s events) → **Diagnose** (LLM investigation) → **Gated action** (human-approved fix via Slack/Discord).

Differentiators vs. K8sGPT/HolmesGPT:
1. It **acts**, behind explicit approval gates.
2. It **proves accuracy** with a public eval harness (verdict + trajectory scores in README).

## 2. Scope (v0 — LOCKED)

In scope:
- One trigger class: pod crash-loop / OOM on a single k3s cluster
- Investigation: logs, events, recent deploys
- Diagnosis + proposed fix posted to Slack/Discord with plain-language explanation
- One-click approve → execute one of three fix types
- Eval harness ships before the agent

Out of scope (v0) — do not build, do not "just add":
- Multi-cluster, multi-trigger, auto-remediation without approval
- Web dashboard, SSO, hosted tier (product-track only, post-traction)
- MCP tool exposure (post-v0 maybe)
- Anything serving neither job track nor product track

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
    tier: draft_only            # v0 launch tier for ALL actions
    graduation_threshold:
      verdict_accuracy: 0.90    # measured on eval harness, per fix type
      min_eval_runs: 20
    reversible: true
  - id: rollback_deployment
    tier: draft_only
    graduation_threshold:
      verdict_accuracy: 0.95
      min_eval_runs: 20
    reversible: true
  - id: bump_resources
    tier: draft_only
    graduation_threshold:
      verdict_accuracy: 0.95
      min_eval_runs: 20
    reversible: true            # revert = reapply previous manifest
gates:
  channel: slack_or_discord
  approve_mechanism: one_click_button
  message_must_include:
    - plain_language_fix        # "Restarting pod X because Y" — Vibe Diff
    - evidence_summary
    - confidence
  timeout_behavior: expire_after_1h_no_action_taken
  audit: every proposal + decision + outcome logged, append-only
```

## 6. Security & identity

```yaml
identity:
  service_account: sre-agent          # dedicated, never default
  rbac:
    read: [pods, pods/log, events, deployments, nodes]
    write: [pods/delete, deployments]  # narrowest verbs that enable the 3 actions
    namespaces: same allowlist as triggers.scope
  secrets: none stored in repo; LLM keys via env only
audit_log:
  location: local append-only file (v0)
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
    verdict_accuracy: agent root cause == known root cause
    trajectory_score: fraction of required_steps completed in order, evidence cited
  reporting:
    destination: README results table
    rule: publish numbers however embarrassing; failure modes are blog content
```

## 8. Non-goals & anti-scope-creep rule

Before adding anything, answer: **job track, product track, or neither?** Cut "neither." If it's product track, it waits for the 90-day traction check (clock starts when M4 ships publicly).

## 9. Amendment log

| Date | Change | Why |
|---|---|---|
| — | v0 initial spec | — |
| 2026-07-06 | Added `pending_unschedulable` trigger to §3 | resource_starvation scenario fires neither original trigger; FailedScheduling is a distinct evidence path (events + node capacity, no logs) that makes trajectory scoring meaningful |
| 2026-07-06 | Added `steps` to §4 output_contract | trajectory scoring needs the ordered log of steps actually run; evidence citations are validated against it (citing a step that never ran zeroes the trajectory score) |

# DESIGN.md — Gated SRE Agent (M1)

> Subordinate to [SPEC.md](SPEC.md): this file records *how* M1 implements the spec. Where the two disagree, SPEC.md wins. Resolves the §10 open design items, plus two components the spec requires but doesn't shape: the trigger watcher and the eval↔agent tool seam.

## 1. Process layout

One container, one Deployment (SPEC §8), three long-lived pieces in one process:

- **trigger watcher** — background task; turns cluster signals into graph runs
- **the graph** — one LangGraph run per trigger firing
- **API server** — FastAPI; approval endpoint, status, reference panel

Single process because the SQLite checkpointer (§6 below) wants a single writer, and nothing here needs independent scaling. Splitting is a non-goal until something forces it.

## 2. Graph: nodes and edges

```
 trigger firing
      │
      ▼
 ┌──────────┐    ┌───────────────┐
 │ observer │───▶│ diagnostician │
 └──────────┘    └───────┬───────┘
                         │
        fix not executable │ fix executable (M1: restart_pod only)
                 ▼         ▼
            ┌───────┐  ┌──────┐ deny / expire
            │ close │  │ gate │───────────────▶ close
            │(draft)│  └──┬───┘
            └───────┘     │ approve
                          ▼
                      ┌───────┐  stale → back to observer (fresh proposal)
                      │  act  │
                      └───┬───┘
                          ▼
                      ┌────────┐  resolved            → close
                      │ verify │  persists / worsened → escalate
                      └────────┘
```

| Node | SPEC | Responsibility |
|---|---|---|
| `observer` | §4, §8 | Runs the four required evidence steps in order via a read-only `EvidenceSource` (§5 below). Appends to `steps`, stores raw output in `evidence`. No LLM, no write access. |
| `diagnostician` | §4, §8 | One structured-output LLM call over the evidence: ranked hypotheses, then verdict (vocab-constrained root cause, confidence, evidence citations) and proposed fix. Has no tools at all — it sees evidence text and produces JSON. This is the trust boundary: attacker-influenceable log content can shape the *proposal*, never an *action*. |
| `gate` | §5 | `interrupt()` with the proposal payload (plain-language fix, evidence summary, confidence, expiry). Resumes with a decision. Deny and expire are ordinary branches: audit + close. |
| `act` | §5, §6 | Pre-flight: proposal not expired, trigger condition still holds (stale-approval re-verify), target namespace in the write allowlist, pod has an ownerReference. Then executes via the `Actuator`. Stale → route back to `observer`. |
| `verify` | §1, §5 | Waits, then re-runs the trigger check plus the relevant evidence step. `resolved` → close; `persists`/`worsened` → escalate with before/after evidence. Never retries without a fresh approval. |
| `close` / `escalate` | §5, §6 | Terminal bookkeeping: audit record, notification. |

The `fix executable` edge checks the §5 actions table: the action must be implemented (M1: `restart_pod`), and `draft_only` actions stop here until they graduate. A run entering `close(draft)` is not a failure; it's the product working as specified. Test affordance: `AGENT_TEST_EXECUTE_NAMESPACES` lets draft_only actions cross the gate in the listed namespaces only, so tests and the walking skeleton can exercise the Act path — production deployments leave it empty; graduation itself remains a SPEC §5 spec amendment, not a config change.

## 3. State schema

```python
class AgentState(TypedDict):
    trigger: Trigger              # {trigger_id, namespace, pod, fired_at, dedup_key}

    # SPEC §4 output_contract — stored verbatim, in contract shape
    steps: list[str]
    verdict: Verdict | None       # {root_cause, confidence, evidence: [step_ids]}
    proposed_fix: ProposedFix | None  # {action_id, params, plain_language}

    evidence: dict[str, str]      # step_id -> raw captured output
    proposal_expires_at: str | None
    decision: Decision | None     # {approved, decided_by, decided_at}
    execution: ExecutionResult | None
    outcome: Outcome | None       # resolved|persists|worsened|denied|expired|stale|draft|error
    model_id: str                 # provenance: which LLM produced the verdict
    prompt_version: str
```

**Invariant:** `steps`, `verdict`, and `proposed_fix` serialize to exactly the §4 `output_contract`, so `eval.score` scores a real agent run with zero harness changes. Any state refactor that breaks this breaks the eval, and the eval is the product (§7).

One graph run per trigger firing; `thread_id` is a fresh UUID per run (the approval endpoint resumes by it). Dedup is the watcher's job, not the graph's.

## 4. Trigger watcher

- Watches pods in `namespaces_watched` (SPEC §3) with the `kubernetes` client's watch API; evaluates the three §3 trigger conditions against pod status and events.
- **Dedup key:** `{trigger_id}/{namespace}/{pod_name}`. At most one active run per key; firings while a run is active (including paused at the gate) are dropped.
- **Debounce** (§3 windows): in-memory `{dedup_key: last_fired}`. Lost on restart — worst case is one duplicate proposal after a crash, which the gate makes harmless. Not worth persisting in M1.
- Not a graph node: it owns no state the checkpointer needs, and it must keep watching while runs are paused.

## 5. Tool layer — the eval↔agent seam

Two narrow protocols, injected at graph construction:

```python
class EvidenceSource(Protocol):        # observer's only capability
    def fetch_pod_logs(self, ns, pod) -> str: ...
    def fetch_events(self, ns, pod) -> str: ...
    def fetch_recent_changes(self, ns) -> str: ...
    def fetch_resources(self, ns, pod) -> str: ...

class Actuator(Protocol):              # act's only capability
    def restart_pod(self, ns, pod) -> ExecutionResult: ...
```

| Implementation | Used by | Backing |
|---|---|---|
| `LiveCluster` | production | `kubernetes` client, in-cluster ServiceAccount (§6) |
| `RecordedEvidence` | eval | the evidence snapshots `eval/runner.py` already captures, keyed by the same §4 step ids |
| `NoopActuator` | eval / observe tier | records the call, touches nothing |

Same graph, same prompts, both modes — the eval scores the artifact that ships. Capability enforcement is structural: `observer` receives only an `EvidenceSource`, `act` only an `Actuator`, `diagnostician` receives neither.

## 6. Checkpointer

SQLite (`langgraph-checkpoint-sqlite`), one file on a small PVC. In-memory for unit tests. Reasons: single writer, single pod, pause-and-resume measured in hours not months, zero operational surface. Postgres is a config swap later if a second writer ever exists; designing for it now buys nothing.

## 7. API

FastAPI, same process. Bearer-token auth with the two SPEC §6 access tiers; M1 uses two static tokens from a Kubernetes Secret (env). How tokens are minted/rotated is deployment-specific and out of scope here.

| Endpoint | Tier | Purpose |
|---|---|---|
| `GET /healthz` | none | liveness |
| `GET /proposals` | observe | runs paused at the gate |
| `GET /proposals/{thread_id}` | observe | full proposal: plain-language fix, evidence summary, confidence, expiry |
| `POST /proposals/{thread_id}/decision` | **act** | `{"approve": bool, "decided_by": str}` — resumes the paused graph |
| `GET /runs/{thread_id}` | observe | state and outcome of any run |

**MCP:** deferred. The decision from SPEC §10 is resolved as *FastAPI first* — the approval endpoint must exist as HTTP for the panel regardless, and an MCP wrapper over these endpoints is a thin later addition; the reverse order isn't.

## 8. Approval surface

A minimal reference panel (single static page served by the API) listing pending proposals and posting decisions — satisfying the §5 contract: full proposal rendered, explicit click, one decision endpoint, expiry respected. Any other act-tier client (an assistant UI, a future chat integration) replaces it by hitting the same endpoint; the agent doesn't know or care which surface approved.

## 9. Notifications and audit

- **Notifier:** one function, two M1 backends — structured log line (always) and optional generic webhook URL from config. Informational only, per §5.
- **Audit:** append-only JSONL file beside the checkpointer DB; one record per run transition with the §6 `audit_log.contents` fields. The audit write happens in `close`/`escalate` and at the gate decision — not best-effort logging, a node responsibility.

## 10. LLM

Provider-agnostic via env (`LLM_MODEL`, key per §6 secrets rule); reference implementation uses the Anthropic API with structured output — the response schema constrains the verdict to the SPEC §7 vocabulary and evidence citations to the §4 step ids, so the output contract holds even against adversarial log content. (Current Anthropic models accept no sampling parameters; determinism is approximated by schema constraint, not temperature.) `model_id` and `prompt_version` are recorded in state and audit, and flow into eval results and the published README table (SPEC §7 provenance).

## 11. Dependencies added for M1

`langgraph`, `langgraph-checkpoint-sqlite`, `fastapi`, `uvicorn`, `anthropic` — on top of the existing `kubernetes` / `PyYAML` / `pytest`. Anything beyond this list needs a reason in this file.

## 12. Deliberately not designed here

Postgres checkpointer, MCP wrapper internals, chat approval surfaces, additional actions (SPEC §5 `deferred_M2`), JIT Kubernetes tokens (SPEC §6 `deferred_hardening`), panel styling.

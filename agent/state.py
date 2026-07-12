"""Graph state (DESIGN §3). SPEC §4 output_contract fields stored verbatim."""

from typing import TypedDict


class Trigger(TypedDict):
    trigger_id: str   # SPEC §3: crashloop | oom | pending_unschedulable
    namespace: str
    pod: str
    fired_at: str
    dedup_key: str


class AgentState(TypedDict, total=False):
    trigger: Trigger

    # SPEC §4 output_contract — serialized unchanged for eval.score
    steps: list
    verdict: dict          # {root_cause, confidence, evidence: [step_ids]}
    proposed_fix: dict     # {action_id, params, plain_language}

    evidence: dict         # step_id -> raw captured output
    hypotheses: list
    proposal_expires_at: str
    decision: dict         # {approved, decided_by, decided_at}
    execution: dict
    outcome: str           # resolved|persists|denied|expired|stale|draft|error
    model_id: str
    prompt_version: str


def output_contract(state):
    """Exactly the SPEC §4 output_contract — what eval.score consumes.

    Breaking this shape breaks the eval, and the eval is the product (SPEC §7).
    """
    return {
        "steps": state.get("steps", []),
        "verdict": state.get("verdict"),
        "proposed_fix": state.get("proposed_fix"),
    }

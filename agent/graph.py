"""The agent graph (DESIGN §2): observer → diagnostician → gate → act → verify.

Dependencies are injected at build time: an EvidenceSource, an Actuator, a
condition_check callable (does the trigger condition still hold?), and the
diagnose function. Same graph in production and eval — only the injected
backings differ (DESIGN §5).
"""

import datetime as dt
import logging
import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent.audit import audit
from agent.config import PROPOSAL_TTL_SECONDS, executable
from agent.llm import PROMPT_VERSION
from agent.state import AgentState, output_contract
from eval.score import REQUIRED_STEPS  # SPEC §4 required investigation order

log = logging.getLogger("agent")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def build_graph(config, evidence_source, actuator, condition_check, diagnose_fn, checkpointer=None):
    def observer(state):
        """Perceive (SPEC §4): run the required evidence steps, in order.

        Overwrites rather than appends — a stale-approval re-entry starts a
        fresh investigation, and per-cycle fields are cleared here.
        """
        trigger = state["trigger"]
        namespace, pod = trigger["namespace"], trigger["pod"]
        calls = {
            "fetch_pod_logs": lambda: evidence_source.fetch_pod_logs(namespace, pod),
            "fetch_events": lambda: evidence_source.fetch_events(namespace, pod),
            "fetch_recent_changes": lambda: evidence_source.fetch_recent_changes(namespace),
            "fetch_resources": lambda: evidence_source.fetch_resources(namespace, pod),
        }
        steps, evidence = [], {}
        for step_id in REQUIRED_STEPS:
            evidence[step_id] = calls[step_id]()
            steps.append(step_id)
        return {"steps": steps, "evidence": evidence,
                "decision": None, "execution": None, "outcome": None}

    def diagnostician(state):
        """Plan (SPEC §4): verdict + proposed fix from evidence text alone."""
        result = diagnose_fn(state["evidence"], state["trigger"], config.model)
        expires = _now() + dt.timedelta(seconds=PROPOSAL_TTL_SECONDS)
        return {
            "steps": state["steps"] + ["hypothesize", "verdict"],
            "hypotheses": result["hypotheses"],
            "verdict": result["verdict"],
            "proposed_fix": result["proposed_fix"],
            "proposal_expires_at": expires.isoformat(),
            "model_id": config.model,
            "prompt_version": PROMPT_VERSION,
        }

    def route_proposal(state):
        fix = state["proposed_fix"]
        if executable(fix["action_id"], state["trigger"]["namespace"], config):
            return "gate"
        return "close_draft"

    def close_draft(state):
        """Not a failure: draft-only proposals stop before the gate (SPEC §5)."""
        return {"outcome": "draft"}

    def gate(state):
        """SPEC §5: interrupt() with the proposal; resumes with a decision."""
        fix, verdict = state["proposed_fix"], state["verdict"]
        decision = interrupt({
            "plain_language_fix": fix["plain_language"],
            "evidence_summary": [h["reasoning"] for h in state.get("hypotheses", [])],
            "confidence": verdict["confidence"],
            "root_cause": verdict["root_cause"],
            "action_id": fix["action_id"],
            "target": f"{state['trigger']['namespace']}/{state['trigger']['pod']}",
            "expires_at": state["proposal_expires_at"],
        })
        return {"decision": {**decision, "decided_at": _now().isoformat()}}

    def act(state):
        """SPEC §5 pre-flight, then the one gated write.

        Targets the triggering pod, never the LLM-proposed params — proposal
        content is downstream of untrusted log text and must not pick targets.
        """
        trigger, decision = state["trigger"], state["decision"]
        if not decision.get("approve"):
            return {"outcome": "denied"}
        if _now() > dt.datetime.fromisoformat(state["proposal_expires_at"]):
            return {"outcome": "expired"}
        if not condition_check(trigger):
            return {"outcome": "stale"}  # cluster moved on: discard, re-propose
        if trigger["namespace"] not in config.namespaces_write:
            return {"outcome": "error",
                    "execution": {"executed": False, "error": "namespace not in write allowlist"}}
        execution = actuator.restart_pod(trigger["namespace"], trigger["pod"])
        if not execution.get("executed"):
            return {"outcome": "error", "execution": execution}
        return {"execution": execution}

    def route_act(state):
        outcome = state.get("outcome")
        if outcome == "stale":
            return "observer"
        return "finalize" if outcome else "verify"

    def verify(state):
        """Post-action Observe (SPEC §1): did the fix actually help?"""
        time.sleep(config.verify_wait_seconds)
        persists = condition_check(state["trigger"])
        return {"outcome": "persists" if persists else "resolved"}

    def finalize(state):
        """Terminal bookkeeping: audit record (SPEC §6) + notification."""
        outcome = state.get("outcome") or "error"
        record = audit(config.audit_path, {
            "trigger": state["trigger"],
            "trajectory": state.get("steps", []),
            "verdict": state.get("verdict"),
            "proposal": state.get("proposed_fix"),
            "human_decision": state.get("decision"),
            "execution_result": state.get("execution"),
            "outcome": outcome,
            "model_id": state.get("model_id"),
            "prompt_version": state.get("prompt_version"),
            "output_contract": output_contract(state),
        })
        if outcome in ("persists", "error"):
            log.error("ESCALATION: %s on %s — %s", outcome, record["trigger"], record["execution_result"])
        else:
            log.info("run closed: %s on %s/%s", outcome,
                     state["trigger"]["namespace"], state["trigger"]["pod"])
        return {"outcome": outcome}

    graph = StateGraph(AgentState)
    graph.add_node("observer", observer)
    graph.add_node("diagnostician", diagnostician)
    graph.add_node("close_draft", close_draft)
    graph.add_node("gate", gate)
    graph.add_node("act", act)
    graph.add_node("verify", verify)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "observer")
    graph.add_edge("observer", "diagnostician")
    graph.add_conditional_edges("diagnostician", route_proposal, ["gate", "close_draft"])
    graph.add_edge("close_draft", "finalize")
    graph.add_edge("gate", "act")
    graph.add_conditional_edges("act", route_act, ["observer", "verify", "finalize"])
    graph.add_edge("verify", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)

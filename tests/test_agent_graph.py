"""Graph behavior tests (DESIGN §2): every SPEC §5 branch, no cluster, no LLM."""

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.config import Config
from agent.graph import build_graph
from agent.state import output_contract
from agent.tools import NoopActuator
from eval.score import score_scenario

TRIGGER = {
    "trigger_id": "oom",
    "namespace": "eval",
    "pod": "victim-oom-leak-abc12-xyz34",
    "fired_at": "2026-07-11T00:00:00+00:00",
    "dedup_key": "oom/eval/victim-oom-leak-abc12-xyz34",
}

DIAGNOSIS = {
    "hypotheses": [{"root_cause": "memory_leak", "reasoning": "restart count climbs with RSS"}],
    "verdict": {"root_cause": "memory_leak", "confidence": 0.85,
                "evidence": ["fetch_pod_logs", "fetch_resources"]},
    "proposed_fix": {"action_id": "restart_pod",
                     "params": {"namespace": "eval", "name": "victim-oom-leak"},
                     "plain_language": "Restart the pod; it leaks until the limit kills it."},
}


class FakeEvidence:
    def fetch_pod_logs(self, namespace, pod):
        return "log line"

    def fetch_events(self, namespace, pod):
        return "[]"

    def fetch_recent_changes(self, namespace):
        return "[]"

    def fetch_resources(self, namespace, pod):
        return "{}"


class FakeActuator(NoopActuator):
    """Records like NoopActuator but reports success, so act proceeds to verify."""

    def restart_pod(self, namespace, pod):
        super().restart_pod(namespace, pod)
        return {"executed": True, "deleted": f"{namespace}/{pod}"}


class ScriptedCondition:
    """condition_check returning scripted values, then the last one forever."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self, trigger, exclude_pod=None):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def make_config(tmp_path, override=()):
    return Config(
        namespaces_watched=("eval",), namespaces_write=("eval",),
        model="test-model", db_path="", audit_path=str(tmp_path / "audit.jsonl"),
        poll_seconds=1, verify_wait_seconds=0,
        execute_override_namespaces=override, observe_token="", act_token="",
    )


def make_graph(tmp_path, override=(), condition=ScriptedCondition(False), actuator=None):
    return build_graph(
        make_config(tmp_path, override),
        evidence_source=FakeEvidence(),
        actuator=actuator or NoopActuator(),
        condition_check=condition,
        diagnose_fn=lambda evidence, trigger, model: dict(DIAGNOSIS),
        checkpointer=MemorySaver(),
    )


def thread(n):
    return {"configurable": {"thread_id": f"t{n}"}}


def test_draft_only_stops_before_gate(tmp_path):
    actuator = NoopActuator()
    graph = make_graph(tmp_path, override=(), actuator=actuator)
    result = graph.invoke({"trigger": TRIGGER}, thread(1))
    assert result["outcome"] == "draft"
    assert "__interrupt__" not in result
    assert actuator.calls == []


def test_output_contract_scores_cleanly(tmp_path):
    graph = make_graph(tmp_path)
    result = graph.invoke({"trigger": TRIGGER}, thread(1))
    scenario = {"id": "x", "known_root_cause": "memory_leak",
                "expected_fix": {"action_id": "restart_pod"}}
    row = score_scenario(output_contract(result), scenario, {"memory_leak", "unknown"})
    assert row["verdict_score"] == 1.0
    assert row["trajectory_score"] == 1.0
    assert row["honesty"] == "correct"


def test_approve_executes_and_verifies(tmp_path):
    actuator = FakeActuator()
    # condition holds at act pre-flight (not stale), gone at verify (resolved)
    graph = make_graph(tmp_path, override=("eval",),
                       condition=ScriptedCondition(True, False), actuator=actuator)
    paused = graph.invoke({"trigger": TRIGGER}, thread(1))
    assert paused["__interrupt__"][0].value["action_id"] == "restart_pod"
    result = graph.invoke(Command(resume={"approve": True, "decided_by": "mel"}), thread(1))
    assert actuator.calls == [("restart_pod", "eval", TRIGGER["pod"])]
    assert result["outcome"] == "resolved"


def test_deny_is_a_real_branch(tmp_path):
    actuator = NoopActuator()
    graph = make_graph(tmp_path, override=("eval",), actuator=actuator)
    graph.invoke({"trigger": TRIGGER}, thread(1))
    result = graph.invoke(Command(resume={"approve": False, "decided_by": "mel"}), thread(1))
    assert result["outcome"] == "denied"
    assert actuator.calls == []


def test_failed_fix_escalates(tmp_path):
    # condition holds at act and still holds at verify -> persists (escalation)
    graph = make_graph(tmp_path, override=("eval",), condition=ScriptedCondition(True),
                       actuator=FakeActuator())
    graph.invoke({"trigger": TRIGGER}, thread(1))
    result = graph.invoke(Command(resume={"approve": True, "decided_by": "mel"}), thread(1))
    assert result["outcome"] == "persists"


def test_stale_approval_reproposes(tmp_path):
    actuator = NoopActuator()
    # condition already gone when approval lands -> discard, re-observe, re-propose
    graph = make_graph(tmp_path, override=("eval",),
                       condition=ScriptedCondition(False), actuator=actuator)
    graph.invoke({"trigger": TRIGGER}, thread(1))
    result = graph.invoke(Command(resume={"approve": True, "decided_by": "mel"}), thread(1))
    assert actuator.calls == []
    assert "__interrupt__" in result  # fresh proposal waiting at the gate


def test_write_allowlist_blocks_execution(tmp_path):
    actuator = NoopActuator()
    trigger = dict(TRIGGER, namespace="kube-system",
                   dedup_key="oom/kube-system/x", pod="x")
    graph = build_graph(
        make_config(tmp_path, override=("kube-system",)),  # override on, allowlist still wins
        evidence_source=FakeEvidence(), actuator=actuator,
        condition_check=ScriptedCondition(True),
        diagnose_fn=lambda evidence, trigger, model: dict(DIAGNOSIS),
        checkpointer=MemorySaver(),
    )
    graph.invoke({"trigger": trigger}, thread(1))
    result = graph.invoke(Command(resume={"approve": True, "decided_by": "mel"}), thread(1))
    assert result["outcome"] == "error"
    assert actuator.calls == []


def test_audit_written_on_close(tmp_path):
    graph = make_graph(tmp_path)
    graph.invoke({"trigger": TRIGGER}, thread(1))
    audit_lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(audit_lines) == 1
    assert '"outcome": "draft"' in audit_lines[0]

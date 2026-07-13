"""API tests (DESIGN §7, §8): tier auth and the reference panel — no cluster."""

import pytest
from fastapi.testclient import TestClient

from agent.api import build_app
from agent.config import Config

PROPOSAL = {
    "plain_language_fix": "Restart the pod; it leaks until the limit kills it.",
    "evidence_summary": ["RSS climbs monotonically"],
    "confidence": 0.85,
    "root_cause": "memory_leak",
    "action_id": "restart_pod",
    "target": "eval/victim",
    "expires_at": "2999-01-01T00:00:00+00:00",
}


class FakeRuntime:
    """Just enough of Runtime for the API: snapshot + resume."""

    def __init__(self, runs):
        self.runs = runs
        self.resumed = []

    def snapshot(self, thread_id=None):
        if thread_id:
            run = self.runs.get(thread_id)
            return dict(run) if run else None
        return {tid: dict(run) for tid, run in self.runs.items()}

    def resume(self, thread_id, decision):
        run = self.runs.get(thread_id)
        if not run or run["status"] != "paused":
            return False
        self.resumed.append((thread_id, decision))
        return True


def make_client(observe_token="obs", act_token="act"):
    config = Config(
        namespaces_watched=("eval",), namespaces_write=("eval",),
        model="test-model", db_path="", audit_path="",
        poll_seconds=1, verify_wait_seconds=0,
        execute_override_namespaces=(), observe_token=observe_token, act_token=act_token,
    )
    runtime = FakeRuntime({
        "t-paused": {"trigger": {}, "status": "paused", "proposal": dict(PROPOSAL), "outcome": None},
        "t-done": {"trigger": {}, "status": "done", "proposal": None, "outcome": "resolved"},
    })
    return TestClient(build_app(runtime, config)), runtime


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_panel_served_unauthenticated():
    client, _ = make_client()
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # the page is static and data-free; proposals arrive only via tokened fetches
    assert "reference approval panel" in response.text
    assert PROPOSAL["plain_language_fix"] not in response.text


def test_observe_tier_required_and_exact():
    client, _ = make_client()
    assert client.get("/proposals").status_code == 401
    # tiers are exact-match, not hierarchical: the act token does not read
    assert client.get("/proposals", headers=auth("act")).status_code == 401
    response = client.get("/proposals", headers=auth("obs"))
    assert response.status_code == 200
    assert set(response.json()) == {"t-paused"}


def test_decision_requires_act_tier():
    client, runtime = make_client()
    body = {"approve": True, "decided_by": "test"}
    assert client.post("/proposals/t-paused/decision", json=body).status_code == 401
    assert client.post("/proposals/t-paused/decision", json=body, headers=auth("obs")).status_code == 401
    response = client.post("/proposals/t-paused/decision", json=body, headers=auth("act"))
    assert response.status_code == 200
    assert runtime.resumed == [("t-paused", body)]


def test_decision_on_non_paused_run_is_409():
    client, _ = make_client()
    body = {"approve": True, "decided_by": "test"}
    assert client.post("/proposals/t-done/decision", json=body, headers=auth("act")).status_code == 409
    assert client.post("/proposals/missing/decision", json=body, headers=auth("act")).status_code == 409


def test_dev_mode_without_tokens_is_open():
    client, _ = make_client(observe_token="", act_token="")
    assert client.get("/proposals").status_code == 200
    assert client.get("/runs/t-done").status_code == 200

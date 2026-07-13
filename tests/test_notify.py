"""Notifier tests (DESIGN §9): both backends, and the runtime transitions
that fire events — no cluster, no LLM."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent.notify import notify
from agent.runtime import Runtime
from tests.test_agent_graph import TRIGGER, make_graph


@pytest.fixture
def webhook_server():
    """Local HTTP server capturing POSTed JSON bodies."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(json.loads(body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", received
    server.shutdown()


def test_notify_posts_json_to_webhook(webhook_server):
    url, received = webhook_server
    notify(url, {"event": "proposal", "confidence": 0.8})
    assert received == [{"event": "proposal", "confidence": 0.8}]


def test_notify_without_webhook_is_log_only():
    notify("", {"event": "outcome"})  # must not raise


def test_notify_survives_dead_webhook():
    notify("http://127.0.0.1:1/unreachable", {"event": "outcome"})  # must not raise


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_runtime_notifies_on_pause_and_outcome(tmp_path):
    """One proposal event at the gate, one outcome event at close — and the
    resume replay of the gate node must not produce a duplicate proposal."""
    events = []
    graph = make_graph(tmp_path, override=("eval",))
    runtime = Runtime(graph, notify_fn=events.append)

    thread_id = runtime.start_run(dict(TRIGGER))
    assert wait_for(lambda: runtime.snapshot(thread_id)["status"] == "paused")
    assert [e["event"] for e in events] == ["proposal"]
    assert events[0]["thread_id"] == thread_id
    # SPEC §5 message_must_include, carried straight from the gate payload
    assert events[0]["plain_language_fix"]
    assert "confidence" in events[0] and "evidence_summary" in events[0]

    runtime.resume(thread_id, {"approve": False, "decided_by": "test"})
    assert wait_for(lambda: runtime.snapshot(thread_id)["status"] == "done")
    assert [e["event"] for e in events] == ["proposal", "outcome"]
    assert events[1]["outcome"] == "denied"
    assert events[1]["target"] == "eval/victim-oom-leak-abc12-xyz34"
    assert events[1]["root_cause"] == "memory_leak"


def test_runtime_notifies_draft_outcome(tmp_path):
    """Draft-only close (no gate) still notifies — pre-graduation deployments
    surface diagnoses through exactly this event."""
    events = []
    graph = make_graph(tmp_path)  # no override: draft path
    runtime = Runtime(graph, notify_fn=events.append)

    thread_id = runtime.start_run(dict(TRIGGER))
    assert wait_for(lambda: runtime.snapshot(thread_id)["status"] == "done")
    assert [e["event"] for e in events] == ["outcome"]
    assert events[0]["outcome"] == "draft"
    assert events[0]["plain_language_fix"]

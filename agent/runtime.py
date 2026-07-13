"""Run registry: one graph run per trigger firing, resumable by thread_id.

The checkpointer owns durable graph state; this registry is the in-process
index the API serves from (which runs exist, which are paused at the gate).
"""

import logging
import threading

from langgraph.types import Command

from agent.watcher import new_thread_id

log = logging.getLogger("agent.runtime")


class Runtime:
    def __init__(self, graph, notify_fn=None):
        self.graph = graph
        self.notify = notify_fn or (lambda event: None)
        self.runs = {}  # thread_id -> {trigger, status, proposal, outcome}
        self.lock = threading.Lock()

    def has_active_run(self, dedup_key):
        with self.lock:
            return any(
                run["trigger"]["dedup_key"] == dedup_key and run["status"] in ("running", "paused")
                for run in self.runs.values()
            )

    def start_run(self, trigger):
        thread_id = new_thread_id()
        with self.lock:
            self.runs[thread_id] = {"trigger": trigger, "status": "running",
                                    "proposal": None, "outcome": None}
        threading.Thread(target=self._invoke, args=(thread_id, {"trigger": trigger}),
                         daemon=True).start()
        return thread_id

    def resume(self, thread_id, decision):
        with self.lock:
            run = self.runs.get(thread_id)
            if not run or run["status"] != "paused":
                return False
            run["status"] = "running"
        threading.Thread(target=self._invoke, args=(thread_id, Command(resume=decision)),
                         daemon=True).start()
        return True

    def _invoke(self, thread_id, payload):
        """Notifications fire here, on run-state transitions, not in graph
        nodes: the gate node body replays on resume (an in-node webhook would
        fire twice per proposal), and the graph stays notification-free for
        eval runs (DESIGN §5: same graph, only the backings differ)."""
        config = {"configurable": {"thread_id": thread_id}}
        try:
            result = self.graph.invoke(payload, config)
        except Exception:
            log.exception("run %s failed", thread_id)
            with self.lock:
                run = self.runs[thread_id]
                run["status"] = "failed"
                trigger = run["trigger"]
            self.notify({"event": "run_failed", "thread_id": thread_id,
                         "target": f"{trigger['namespace']}/{trigger['pod']}"})
            return
        with self.lock:
            run = self.runs[thread_id]
            interrupts = result.get("__interrupt__")
            if interrupts:
                run["status"] = "paused"
                run["proposal"] = interrupts[0].value
                event = {"event": "proposal", "thread_id": thread_id, **run["proposal"]}
            else:
                run["status"] = "done"
                run["outcome"] = result.get("outcome")
                run["proposal"] = None
                trigger = run["trigger"]
                verdict = result.get("verdict") or {}
                fix = result.get("proposed_fix") or {}
                event = {
                    "event": "outcome", "thread_id": thread_id,
                    "outcome": run["outcome"],
                    "target": f"{trigger['namespace']}/{trigger['pod']}",
                    "root_cause": verdict.get("root_cause"),
                    "confidence": verdict.get("confidence"),
                    "plain_language_fix": fix.get("plain_language"),
                }
        self.notify(event)

    def snapshot(self, thread_id=None):
        with self.lock:
            if thread_id:
                run = self.runs.get(thread_id)
                return dict(run) if run else None
            return {tid: dict(run) for tid, run in self.runs.items()}

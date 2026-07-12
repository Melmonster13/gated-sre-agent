"""Trigger watcher (DESIGN §4): turns SPEC §3 conditions into graph runs.

Polls pod state in the watched namespaces. Reuses the harness's condition
helpers so the agent and the eval seed/detect failures identically. Debounce
is in-memory — worst case after a crash is one duplicate proposal, which the
gate absorbs.
"""

import datetime as dt
import logging
import time
import uuid

# Same SPEC §3 condition logic the eval harness uses (eval/runner.py).
from eval.runner import DEBOUNCE_SECONDS, _failed_scheduling, _oom_killed, _waiting_failure

log = logging.getLogger("agent.watcher")


def classify(core, pod, pending_names):
    """Map a pod's state to a SPEC §3 trigger id, or None."""
    if _oom_killed(pod):
        return "oom"
    if _waiting_failure(pod):
        return "crashloop"
    if pod.status.phase == "Pending" and _failed_scheduling(core, pending_names):
        return "pending_unschedulable"
    return None


class Watcher:
    def __init__(self, core_api, config, start_run, has_active_run):
        self.core = core_api
        self.config = config
        self.start_run = start_run          # callable(trigger dict)
        self.has_active_run = has_active_run  # callable(dedup_key) -> bool
        self.last_fired = {}                # dedup_key -> monotonic seconds
        self.stop = False

    def scan_once(self):
        for namespace in self.config.namespaces_watched:
            try:
                pods = self.core.list_namespaced_pod(namespace).items
            except Exception as exc:
                log.warning("cannot list pods in %s: %s", namespace, exc)
                continue
            pending = {p.metadata.name for p in pods if p.status.phase == "Pending"}
            for pod in pods:
                trigger_id = classify(self.core, pod, pending)
                if trigger_id:
                    self._fire(trigger_id, namespace, pod.metadata.name)

    def _fire(self, trigger_id, namespace, pod):
        dedup_key = f"{trigger_id}/{namespace}/{pod}"
        if self.has_active_run(dedup_key):
            return
        debounce = DEBOUNCE_SECONDS[trigger_id]
        now = time.monotonic()
        if now - self.last_fired.get(dedup_key, -debounce) < debounce:
            return
        self.last_fired[dedup_key] = now
        log.info("trigger %s fired", dedup_key)
        self.start_run({
            "trigger_id": trigger_id,
            "namespace": namespace,
            "pod": pod,
            "fired_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "dedup_key": dedup_key,
        })

    def run(self):
        while not self.stop:
            self.scan_once()
            time.sleep(self.config.poll_seconds)


def make_condition_check(core_api):
    """Does the trigger's failure condition still hold? Used by act (stale
    re-verify) and verify (did the fix help?) — SPEC §5."""

    def condition_check(trigger, exclude_pod=None):
        try:
            pods = core_api.list_namespaced_pod(trigger["namespace"]).items
        except Exception:
            return False
        pending = {p.metadata.name for p in pods if p.status.phase == "Pending"}
        # The condition holds if some pod from the same workload still shows
        # the failure. Verify excludes the pod the fix just deleted — its
        # Terminating carcass still carries the failure status and says
        # nothing about whether the fix worked.
        prefix = trigger["pod"].rsplit("-", 2)[0]
        return any(
            classify(core_api, pod, pending) == trigger["trigger_id"]
            for pod in pods
            if pod.metadata.name.rsplit("-", 2)[0] == prefix
            and pod.metadata.name != exclude_pod
        )

    return condition_check


def new_thread_id():
    return uuid.uuid4().hex

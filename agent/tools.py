"""Tool layer (DESIGN §5) — the eval↔agent seam.

Capability enforcement is structural: the observer node receives only an
EvidenceSource, the act node only an Actuator, and the diagnostician neither.
Each fetch returns a JSON string; the diagnostician sees text, never a client.
"""

import datetime as dt
import json
from typing import Protocol

EVENT_WINDOW_MINUTES = 30   # SPEC §4 fetch_events
LOG_TAIL_LINES = 200        # SPEC §4 fetch_pod_logs
RECENT_CHANGE_HOURS = 24    # SPEC §4 fetch_recent_changes


class EvidenceSource(Protocol):
    def fetch_pod_logs(self, namespace, pod) -> str: ...
    def fetch_events(self, namespace, pod) -> str: ...
    def fetch_recent_changes(self, namespace) -> str: ...
    def fetch_resources(self, namespace, pod) -> str: ...


class Actuator(Protocol):
    def restart_pod(self, namespace, pod) -> dict: ...


class RecordedEvidence:
    """Evidence from an eval/runner.py snapshot (state.json) — eval mode.

    The runner captures exactly the SPEC §4 evidence set keyed by the
    required_steps ids, so this is a straight lookup.
    """

    def __init__(self, state_path):
        self.evidence = json.loads(state_path.read_text())["evidence"]

    def _get(self, step_id):
        return json.dumps(self.evidence.get(step_id), indent=1, default=str)

    def fetch_pod_logs(self, namespace, pod):
        return self._get("fetch_pod_logs")

    def fetch_events(self, namespace, pod):
        return self._get("fetch_events")

    def fetch_recent_changes(self, namespace):
        return self._get("fetch_recent_changes")

    def fetch_resources(self, namespace, pod):
        return self._get("fetch_resources")


class NoopActuator:
    """Records the call, touches nothing — eval mode and observe tier."""

    def __init__(self):
        self.calls = []

    def restart_pod(self, namespace, pod):
        self.calls.append(("restart_pod", namespace, pod))
        return {"executed": False, "noop": True}


class LiveCluster:
    """EvidenceSource backed by the kubernetes client (read-only calls only)."""

    def __init__(self, core_api, apps_api):
        self.core = core_api
        self.apps = apps_api

    def fetch_pod_logs(self, namespace, pod):
        logs = {}
        for previous in (False, True):
            key = "previous" if previous else "current"
            try:
                logs[key] = self.core.read_namespaced_pod_log(
                    pod, namespace, tail_lines=LOG_TAIL_LINES, previous=previous
                )
            except Exception as exc:
                logs[key] = f"<unavailable: {exc.__class__.__name__}>"
        return json.dumps(logs)

    def fetch_events(self, namespace, pod):
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=EVENT_WINDOW_MINUTES)
        events = []
        for event in self.core.list_namespaced_event(namespace).items:
            seen = event.last_timestamp or event.event_time or event.first_timestamp
            if seen and seen < cutoff:
                continue
            events.append({
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "object": f"{event.involved_object.kind}/{event.involved_object.name}",
                "last_seen": str(seen),
            })
        return json.dumps(events, indent=1)

    def fetch_recent_changes(self, namespace):
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=RECENT_CHANGE_HOURS)
        changes = []
        for deploy in self.apps.list_namespaced_deployment(namespace).items:
            updated = max(
                (c.last_update_time for c in deploy.status.conditions or [] if c.last_update_time),
                default=None,
            )
            if updated and updated < cutoff:
                continue
            containers = deploy.spec.template.spec.containers
            changes.append({
                "deployment": deploy.metadata.name,
                "images": [c.image for c in containers],
                "replicas": deploy.status.ready_replicas or 0,
                "updated": str(updated),
            })
        return json.dumps(changes, indent=1)

    def fetch_resources(self, namespace, pod):
        detail = {"pod": pod, "containers": [], "nodes": []}
        try:
            spec = self.core.read_namespaced_pod(pod, namespace).spec
            for container in spec.containers:
                detail["containers"].append({
                    "name": container.name,
                    "requests": (container.resources.requests or {}) if container.resources else {},
                    "limits": (container.resources.limits or {}) if container.resources else {},
                })
        except Exception as exc:
            detail["pod_error"] = str(exc)
        for node in self.core.list_node().items:
            detail["nodes"].append({
                "name": node.metadata.name,
                "allocatable": {k: node.status.allocatable[k] for k in ("cpu", "memory")},
            })
        return json.dumps(detail, indent=1)


class LiveActuator:
    """The one gated write (SPEC §6). Delete the pod; the controller recreates it."""

    def __init__(self, core_api):
        self.core = core_api

    def restart_pod(self, namespace, pod):
        try:
            target = self.core.read_namespaced_pod(pod, namespace)
        except Exception as exc:
            return {"executed": False, "error": f"pod not found: {exc.__class__.__name__}"}
        if not target.metadata.owner_references:
            # SPEC §5 guard: a bare pod would not come back
            return {"executed": False, "error": "refused: pod has no ownerReference"}
        self.core.delete_namespaced_pod(pod, namespace)
        return {"executed": True, "deleted": f"{namespace}/{pod}"}

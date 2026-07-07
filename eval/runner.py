"""Eval harness runner (SPEC §7).

Per scenario: apply the setup manifest to the `eval` namespace, poll until the
wait_for condition is met (or times out), capture the SPEC §4 evidence set as
state.json, then tear down. Teardown deletes by label selector and runs in a
finally block, so a failed or Ctrl-C'd run never leaks resources.

Usage:
    python -m eval.runner --scenario crashloop_bad_image
    python -m eval.runner --all --debounce-override 0
"""

import argparse
import datetime as dt
import json
import subprocess
import sys
import time

import yaml
from kubernetes import client, config, utils
from kubernetes.client.rest import ApiException

from eval.common import RUNS_DIR, load_scenarios

NAMESPACE = "eval"
LOG_TAIL_LINES = 200        # SPEC §4 fetch_pod_logs
EVENT_WINDOW_MINUTES = 30   # SPEC §4 fetch_events
POLL_SECONDS = 5
CLEANUP_TIMEOUT_SECONDS = 60

# SPEC §3 debounce windows, keyed by trigger id. The real agent waits this
# long before re-firing on the same pod; the runner mirrors it between the
# condition being met and the snapshot, so eval evidence matches what the
# agent would actually see.
DEBOUNCE_SECONDS = {"crashloop": 120, "oom": 120, "pending_unschedulable": 300}

# Waiting reasons that count as the crash-loop failure class. ImagePullBackOff
# and CreateContainerConfigError are waiting reasons, not crashes — the
# scenarios rely on catching them here (see crashloop_bad_image notes).
FAILURE_WAITING_REASONS = {
    "ImagePullBackOff",
    "ErrImagePull",
    "CrashLoopBackOff",
    "CreateContainerConfigError",
}


def ensure_namespace(core):
    try:
        core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=NAMESPACE)))
    except ApiException as exc:
        if exc.status != 409:  # already exists
            raise


def teardown(scenario):
    """Idempotent: deletes by label selector, ignores what's already gone."""
    subprocess.run(scenario["teardown"], shell=True, check=False)


def wait_for_scenario_gone(apps, scenario_id):
    """Block until leftovers from a previous run are deleted, so setup can't 409."""
    selector = f"eval-scenario={scenario_id}"
    deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not apps.list_namespaced_deployment(NAMESPACE, label_selector=selector).items:
            return
        time.sleep(2)
    raise RuntimeError(f"leftover {scenario_id} resources did not clear in {CLEANUP_TIMEOUT_SECONDS}s")


def apply_setup(api, scenario):
    for doc in yaml.safe_load_all(scenario["setup_manifest"]):
        if doc:
            utils.create_from_dict(api, doc, namespace=NAMESPACE)


# --- wait_for conditions -----------------------------------------------------
# Deliberately hardcoded per condition type (no condition DSL): the three
# failure shapes in SPEC §3 are waiting reasons, OOMKilled, and
# Pending + FailedScheduling.


def scenario_pods(core, scenario_id):
    return core.list_namespaced_pod(NAMESPACE, label_selector=f"eval-scenario={scenario_id}").items


def _oom_killed(pod):
    for cs in pod.status.container_statuses or []:
        for state in (cs.state, cs.last_state):
            if state and state.terminated and state.terminated.reason == "OOMKilled":
                return True
    return False


def _waiting_failure(pod):
    for cs in pod.status.container_statuses or []:
        if cs.state and cs.state.waiting and cs.state.waiting.reason in FAILURE_WAITING_REASONS:
            return True
    return False


def _failed_scheduling(core, pod_names):
    for event in core.list_namespaced_event(NAMESPACE).items:
        if event.reason == "FailedScheduling" and event.involved_object.name in pod_names:
            return True
    return False


def condition_met(core, scenario):
    condition = scenario["wait_for"]["condition"]
    pods = scenario_pods(core, scenario["id"])
    if "OOMKilled" in condition:
        return any(_oom_killed(p) for p in pods)
    if "Pending" in condition:
        pending = {p.metadata.name for p in pods if p.status.phase == "Pending"}
        return bool(pending) and _failed_scheduling(core, pending)
    return any(_waiting_failure(p) for p in pods)


def wait_for_failure(core, scenario):
    timeout = scenario["wait_for"]["timeout_seconds"]
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if condition_met(core, scenario):
            return True, time.monotonic() - start
        time.sleep(POLL_SECONDS)
    return False, time.monotonic() - start


# --- snapshot ----------------------------------------------------------------
# Exactly the SPEC §4 evidence set, keyed by the required_steps ids so the
# agent's step log and the snapshot speak the same names.


def _read_log(core, pod_name, container_name, previous):
    try:
        return core.read_namespaced_pod_log(
            pod_name, NAMESPACE, container=container_name,
            tail_lines=LOG_TAIL_LINES, previous=previous,
        )
    except ApiException:
        return None  # no logs yet, or no previous container — that absence is evidence too


def _pod_logs(core, pod):
    return {
        container.name: {
            "current": _read_log(core, pod.metadata.name, container.name, previous=False),
            "previous": _read_log(core, pod.metadata.name, container.name, previous=True),
        }
        for container in pod.spec.containers
    }


def _recent_events(core):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=EVENT_WINDOW_MINUTES)
    events = []
    for event in core.list_namespaced_event(NAMESPACE).items:
        stamp = event.last_timestamp or event.event_time or event.metadata.creation_timestamp
        if stamp and stamp < cutoff:
            continue
        events.append({
            "type": event.type,
            "reason": event.reason,
            "message": event.message,
            "count": event.count,
            "object": f"{event.involved_object.kind}/{event.involved_object.name}",
            "last_seen": str(stamp),
        })
    return events


def _deployments(apps):
    return [
        {
            "name": d.metadata.name,
            "created": str(d.metadata.creation_timestamp),
            "images": [c.image for c in d.spec.template.spec.containers],
            "replicas": d.spec.replicas,
            "ready_replicas": d.status.ready_replicas,
            "conditions": [
                {
                    "type": c.type,
                    "status": c.status,
                    "reason": c.reason,
                    "message": c.message,
                    "last_update": str(c.last_update_time),
                }
                for c in d.status.conditions or []
            ],
        }
        for d in apps.list_namespaced_deployment(NAMESPACE).items
    ]


def _pod_resources(pod):
    return {
        c.name: {"requests": c.resources.requests or {}, "limits": c.resources.limits or {}}
        for c in pod.spec.containers
    }


def _node_allocatable(core):
    return [
        {"name": n.metadata.name, "allocatable": n.status.allocatable}
        for n in core.list_node().items
    ]


def capture_snapshot(core, apps, scenario_id):
    pods = scenario_pods(core, scenario_id)
    return {
        "fetch_pod_logs": {p.metadata.name: _pod_logs(core, p) for p in pods},
        "fetch_events": _recent_events(core),
        "fetch_recent_changes": _deployments(apps),
        "fetch_resources": {
            "pods": {p.metadata.name: _pod_resources(p) for p in pods},
            "nodes": _node_allocatable(core),
        },
    }


# --- orchestration -----------------------------------------------------------


def run_scenario(api, core, apps, scenario, run_dir, debounce_override=None):
    scenario_id = scenario["id"]
    print(f"[{scenario_id}] setting up")
    ensure_namespace(core)
    teardown(scenario)  # clear leftovers from an earlier crashed run
    wait_for_scenario_gone(apps, scenario_id)
    try:
        apply_setup(api, scenario)
        condition = scenario["wait_for"]["condition"]
        print(f"[{scenario_id}] waiting up to {scenario['wait_for']['timeout_seconds']}s for: {condition}")
        met, waited = wait_for_failure(core, scenario)
        if met:
            debounce = (
                DEBOUNCE_SECONDS[scenario["trigger_expected"]]
                if debounce_override is None
                else debounce_override
            )
            if debounce:
                print(f"[{scenario_id}] condition met; debouncing {debounce}s (SPEC §3 — use --debounce-override 0 to skip)")
                time.sleep(debounce)
        else:
            print(f"[{scenario_id}] TIMED OUT after {waited:.0f}s — snapshotting anyway", file=sys.stderr)

        snapshot = {
            "scenario_id": scenario_id,
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "wait_result": {"condition": condition, "met": met, "waited_seconds": round(waited, 1)},
            "evidence": capture_snapshot(core, apps, scenario_id),
        }
        out_dir = run_dir / scenario_id
        out_dir.mkdir(parents=True, exist_ok=True)
        state_path = out_dir / "state.json"
        state_path.write_text(json.dumps(snapshot, indent=2, default=str))
        print(f"[{scenario_id}] wrote {state_path}")
        return met
    finally:
        print(f"[{scenario_id}] tearing down")
        teardown(scenario)


def main():
    parser = argparse.ArgumentParser(description="Seed eval scenarios into the cluster and snapshot the evidence.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--scenario", help="run a single scenario by id")
    which.add_argument("--all", action="store_true", help="run every scenario")
    parser.add_argument(
        "--debounce-override", type=int, default=None, metavar="SECONDS",
        help="override the SPEC §3 debounce wait (0 for fast iteration)",
    )
    args = parser.parse_args()

    scenarios = load_scenarios()
    if args.all:
        selected = list(scenarios.values())
    elif args.scenario in scenarios:
        selected = [scenarios[args.scenario]]
    else:
        raise SystemExit(f"unknown scenario {args.scenario!r}; known: {', '.join(scenarios)}")

    config.load_kube_config()  # honors KUBECONFIG
    api = client.ApiClient()
    core = client.CoreV1Api(api)
    apps = client.AppsV1Api(api)

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True)
    print(f"run {run_id} -> {run_dir}")

    timed_out = []
    for scenario in selected:
        if not run_scenario(api, core, apps, scenario, run_dir, args.debounce_override):
            timed_out.append(scenario["id"])

    if timed_out:
        print(f"scenarios that never reached their failure condition: {', '.join(timed_out)}", file=sys.stderr)
        sys.exit(1)
    print(f"run {run_id} complete")


if __name__ == "__main__":
    main()

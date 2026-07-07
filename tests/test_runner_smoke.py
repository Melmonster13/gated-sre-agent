"""Integration smoke test — needs a live cluster, skipped unless KUBECONFIG is set."""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("KUBECONFIG"), reason="KUBECONFIG not set; needs a live cluster"
)


def test_runner_end_to_end(tmp_path):
    from kubernetes import client, config

    from eval import runner
    from eval.common import load_scenarios

    config.load_kube_config()
    api = client.ApiClient()
    core = client.CoreV1Api(api)
    apps = client.AppsV1Api(api)

    scenario = load_scenarios()["crashloop_bad_image"]
    met = runner.run_scenario(api, core, apps, scenario, tmp_path, debounce_override=0)
    assert met, "ImagePullBackOff never showed up"

    state = json.loads((tmp_path / "crashloop_bad_image" / "state.json").read_text())
    assert state["wait_result"]["met"]
    assert set(state["evidence"]) == {
        "fetch_pod_logs", "fetch_events", "fetch_recent_changes", "fetch_resources",
    }
    assert any("victim-bad-image" in d["name"] for d in state["evidence"]["fetch_recent_changes"])

    # teardown ran: the deployment is gone (pods may still be terminating)
    leftovers = apps.list_namespaced_deployment(
        runner.NAMESPACE, label_selector="eval-scenario=crashloop_bad_image"
    ).items
    assert not leftovers

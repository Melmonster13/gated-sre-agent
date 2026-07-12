"""Run the real agent over a recorded eval run (DESIGN §5, eval mode).

Same graph as production, backed by RecordedEvidence + NoopActuator. All
actions are draft_only (SPEC §5) so every run closes as a draft before the
gate — exactly what scoring needs. Writes agent_output.json per scenario in
the SPEC §4 contract shape; score with `python -m eval.score --run <id>`.

Usage:
    python -m agent.evalrun --run latest
"""

import argparse
import dataclasses
import json

from agent.config import load_config
from agent.graph import build_graph
from agent.llm import diagnose
from agent.state import output_contract
from agent.tools import NoopActuator, RecordedEvidence
from eval.common import load_scenarios, resolve_run_dir


def main():
    parser = argparse.ArgumentParser(description="Write real-agent outputs for every scenario in a run.")
    parser.add_argument("--run", default="latest", help="run id under eval/runs/ (default: latest)")
    args = parser.parse_args()

    run_dir = resolve_run_dir(args.run)
    cfg = dataclasses.replace(load_config(), audit_path=str(run_dir / "agent_audit.jsonl"))

    for scenario_id, scenario in load_scenarios().items():
        state_path = run_dir / scenario_id / "state.json"
        if not state_path.exists():
            print(f"skipping {scenario_id}: no {state_path}")
            continue
        graph = build_graph(
            cfg,
            evidence_source=RecordedEvidence(state_path),
            actuator=NoopActuator(),
            condition_check=lambda trigger: False,
            diagnose_fn=diagnose,
        )
        trigger = {
            "trigger_id": scenario["trigger_expected"],
            "namespace": "eval",
            "pod": scenario["expected_fix"]["params"].get("deployment", scenario_id),
            "fired_at": "",
            "dedup_key": f"eval/{scenario_id}",
        }
        result = graph.invoke({"trigger": trigger})
        output = {**output_contract(result), "provenance": {
            "agent": "gated-sre-agent",
            "model_id": result.get("model_id"),
            "prompt_version": result.get("prompt_version"),
        }}
        output_path = run_dir / scenario_id / "agent_output.json"
        output_path.write_text(json.dumps(output, indent=2))
        print(f"wrote {output_path} (outcome={result.get('outcome')})")


if __name__ == "__main__":
    main()

"""Deliberately dumb baseline agent — the "before" picture.

Skips investigation entirely and always blames the memory limit with high
confidence. Expected scores: ~20% verdict accuracy (right only on
oom_limit_too_low), 0% trajectory, high confident-lie rate. The real agent
has to beat this to justify existing.

Usage:
    python -m eval.baseline --run latest
"""

import argparse
import json

from eval.common import load_scenarios, resolve_run_dir


def diagnose(_state):
    """Ignores the evidence. That's the point."""
    return {
        "steps": [],
        "verdict": {"root_cause": "limit_too_low", "confidence": 0.9, "evidence": []},
        "proposed_fix": {
            "action_id": "restart_pod",
            "params": {},
            "plain_language": "Restarting the pod; it is probably out of memory.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Write baseline agent outputs for every scenario in a run.")
    parser.add_argument("--run", default="latest", help="run id under eval/runs/ (default: latest)")
    args = parser.parse_args()

    run_dir = resolve_run_dir(args.run)
    for scenario_id in load_scenarios():
        state_path = run_dir / scenario_id / "state.json"
        if not state_path.exists():
            print(f"skipping {scenario_id}: no {state_path}")
            continue
        output = diagnose(json.loads(state_path.read_text()))
        output_path = run_dir / scenario_id / "agent_output.json"
        output_path.write_text(json.dumps(output, indent=2))
        print(f"wrote {output_path}")


if __name__ == "__main__":
    main()

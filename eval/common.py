"""Shared paths and loaders for the eval harness."""

from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = EVAL_DIR / "scenarios"
RUNS_DIR = EVAL_DIR / "runs"


def load_scenarios():
    """Return {scenario_id: scenario dict} for every scenario YAML."""
    scenarios = {}
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        if path.name == "vocab.yaml":
            continue
        scenario = yaml.safe_load(path.read_text())
        scenarios[scenario["id"]] = scenario
    return scenarios


def load_vocab():
    """Return the controlled root-cause vocabulary as a set of strings."""
    return set(yaml.safe_load((SCENARIOS_DIR / "vocab.yaml").read_text())["root_causes"])


def resolve_run_dir(run):
    """Map a run id (or 'latest') to its directory under eval/runs/."""
    if run != "latest":
        run_dir = RUNS_DIR / run
        if not run_dir.is_dir():
            raise SystemExit(f"no such run: {run_dir}")
        return run_dir
    runs = sorted(p for p in RUNS_DIR.iterdir() if p.is_dir()) if RUNS_DIR.is_dir() else []
    if not runs:
        raise SystemExit("no runs under eval/runs/ — run `python -m eval.runner --all` first")
    return runs[-1]

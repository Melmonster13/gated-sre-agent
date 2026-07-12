"""Regenerate the README eval results table from the latest results.json.

The table between the markers is never hand-edited (SPEC §7: publish numbers
however embarrassing).

Usage:
    python -m eval.update_readme
"""

import argparse
import json

from eval.common import EVAL_DIR, resolve_run_dir

README_PATH = EVAL_DIR.parent / "README.md"
START_MARKER = "<!-- EVAL_RESULTS_START -->"
END_MARKER = "<!-- EVAL_RESULTS_END -->"


def render_provenance(results):
    parts = []
    for prov in results.get("provenance", [{"agent": "unknown"}]):
        desc = f"`{prov.get('agent', 'unknown')}`"
        if prov.get("model_id"):
            desc += f" ({prov['model_id']}, prompt {prov.get('prompt_version')})"
        parts.append(desc)
    return ", ".join(parts)


def render_table(results):
    lines = [
        f"Run `{results['run_id']}` — scored {results['scored_at'][:10]} — "
        f"produced by {render_provenance(results)}",
        "",
        "| Scenario | Known root cause | Agent verdict | Verdict | Trajectory | Honesty |",
        "|---|---|---|---|---|---|",
    ]
    for row in results["scenarios"]:
        mark = "✅" if row["verdict_score"] == 1 else "❌"
        lines.append(
            f"| {row['scenario_id']} | {row['known_root_cause']} | {row['agent_root_cause']} "
            f"| {mark} | {row['trajectory_score']:.2f} | {row['honesty']} |"
        )
    s = results["summary"]
    lines += [
        "",
        f"**Summary:** verdict accuracy {s['verdict_acc']:.0%} · trajectory avg {s['trajectory_avg']:.2f} "
        f"· confident-lie rate {s['lie_rate']:.0%} · unknown rate {s['unknown_rate']:.0%}",
    ]
    return "\n".join(lines)


def splice(readme_text, table):
    start = readme_text.find(START_MARKER)
    end = readme_text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise SystemExit(f"README.md is missing the {START_MARKER} / {END_MARKER} markers")
    head = readme_text[: start + len(START_MARKER)]
    tail = readme_text[end:]
    return f"{head}\n{table}\n{tail}"


def main():
    parser = argparse.ArgumentParser(description="Rewrite the README results table from a run's results.json.")
    parser.add_argument("--run", default="latest", help="run id under eval/runs/ (default: latest)")
    args = parser.parse_args()

    run_dir = resolve_run_dir(args.run)
    results_path = run_dir / "results.json"
    if not results_path.exists():
        raise SystemExit(f"no {results_path} — run `python -m eval.score --run {run_dir.name}` first")

    results = json.loads(results_path.read_text())
    README_PATH.write_text(splice(README_PATH.read_text(), render_table(results)))
    print(f"updated {README_PATH} from {results_path}")


if __name__ == "__main__":
    main()

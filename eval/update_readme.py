"""Regenerate the README eval results from every scored run.

The block between the markers is never hand-edited (SPEC §7: publish numbers
however embarrassing). Every run under eval/runs/ with a results.json is
published: a comparison row per run, then per-scenario detail per run — so
re-running the pipeline appends runs rather than replacing the published one.

Usage:
    python -m eval.update_readme                 # all scored runs
    python -m eval.update_readme --runs id1,id2  # explicit subset, in order
"""

import argparse
import json

from eval.common import EVAL_DIR, RUNS_DIR, resolve_run_dir

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


def render_comparison(all_results):
    lines = [
        "| Run | Agent | Verdict accuracy | Trajectory avg | Confident-lie | Unknown |",
        "|---|---|---|---|---|---|",
    ]
    for results in all_results:
        s = results["summary"]
        lines.append(
            f"| `{results['run_id']}` | {render_provenance(results)} "
            f"| {s['verdict_acc']:.0%} | {s['trajectory_avg']:.2f} "
            f"| {s['lie_rate']:.0%} | {s['unknown_rate']:.0%} |"
        )
    return "\n".join(lines)


def render_run(results):
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
    return "\n".join(lines)


def render_block(all_results):
    sections = [render_comparison(all_results)]
    sections += [render_run(results) for results in all_results]
    return "\n\n".join(sections)


def splice(readme_text, block):
    start = readme_text.find(START_MARKER)
    end = readme_text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise SystemExit(f"README.md is missing the {START_MARKER} / {END_MARKER} markers")
    head = readme_text[: start + len(START_MARKER)]
    tail = readme_text[end:]
    return f"{head}\n{block}\n{tail}"


def scored_run_dirs(runs_arg):
    if runs_arg:
        return [resolve_run_dir(run_id.strip()) for run_id in runs_arg.split(",")]
    if not RUNS_DIR.is_dir():
        raise SystemExit(f"no runs under {RUNS_DIR}")
    return sorted(p for p in RUNS_DIR.iterdir() if (p / "results.json").exists())


def main():
    parser = argparse.ArgumentParser(description="Rewrite the README results block from scored runs.")
    parser.add_argument("--runs", default="",
                        help="comma-separated run ids (default: every run with a results.json)")
    args = parser.parse_args()

    all_results = []
    for run_dir in scored_run_dirs(args.runs):
        results_path = run_dir / "results.json"
        if not results_path.exists():
            raise SystemExit(f"no {results_path} — run `python -m eval.score --run {run_dir.name}` first")
        all_results.append(json.loads(results_path.read_text()))
    if not all_results:
        raise SystemExit("no scored runs to publish")

    README_PATH.write_text(splice(README_PATH.read_text(), render_block(all_results)))
    print(f"updated {README_PATH} from {len(all_results)} run(s)")


if __name__ == "__main__":
    main()

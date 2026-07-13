"""README publishing tests: multi-run rendering and marker splicing."""

import pytest

from eval.update_readme import END_MARKER, START_MARKER, render_block, splice

RESULTS_A = {
    "run_id": "r1", "scored_at": "2026-07-12T00:00:00",
    "provenance": [{"agent": "gated-sre-agent", "model_id": "model-a", "prompt_version": "v1"}],
    "summary": {"verdict_acc": 1.0, "trajectory_avg": 1.0, "lie_rate": 0.0, "unknown_rate": 0.0},
    "scenarios": [{"scenario_id": "s", "known_root_cause": "bad_image", "agent_root_cause": "bad_image",
                   "verdict_score": 1.0, "trajectory_score": 1.0, "honesty": "correct"}],
}

RESULTS_B = {
    "run_id": "r2", "scored_at": "2026-07-13T00:00:00",
    "provenance": [{"agent": "gated-sre-agent", "model_id": "model-b", "prompt_version": "v1"}],
    "summary": {"verdict_acc": 0.8, "trajectory_avg": 1.0, "lie_rate": 0.2, "unknown_rate": 0.0},
    "scenarios": [{"scenario_id": "s", "known_root_cause": "bad_image", "agent_root_cause": "bad_config",
                   "verdict_score": 0.0, "trajectory_score": 1.0, "honesty": "confident_lie"}],
}


def test_block_has_one_comparison_row_and_one_detail_section_per_run():
    block = render_block([RESULTS_A, RESULTS_B])
    assert block.count("| `r1` |") == 1 and block.count("| `r2` |") == 1
    assert "model-a" in block and "model-b" in block
    assert block.count("Run `r") == 2  # per-run detail headers
    assert "| 80% | 1.00 | 20% | 0% |" in block  # comparison row carries the summary


def test_splice_replaces_only_between_markers():
    readme = f"intro\n{START_MARKER}\nold\n{END_MARKER}\noutro"
    spliced = splice(readme, "new block")
    assert "old" not in spliced
    assert spliced.startswith("intro") and spliced.endswith("outro")
    assert "new block" in spliced


def test_splice_without_markers_fails_loudly():
    with pytest.raises(SystemExit):
        splice("no markers here", "block")

"""Tests for the Phase 11 backtest: replaying held-out commits through the real
reading-plan code, on the synthetic dataset from test_train (no network)."""

import pytest
from typer.testing import CliRunner

from review_map.cli import app
from review_map.config import ReadingBudget
from review_map.models import RiskLevel
from review_map.report.plan import Candidate, select_sections
from review_map.risk.features import build_features
from review_map.risk.scorer import ScoringModel, folder_reader
from review_map.training import backtest as bt
from review_map.training import evaluate as ev
from tests.test_train import synthetic_commits, synthetic_rows, write_dataset

runner = CliRunner()


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    """A model trained by `evaluate` on the synthetic data and saved like `train` does."""
    rows = synthetic_rows()
    commits = synthetic_commits(rows)
    run = ev.run_evaluation(rows, commits)
    folder = tmp_path_factory.mktemp("model")
    run.trained.model.metrics = ev.metrics_json(run)
    run.trained.model.save(folder, ev.model_card(run, "synthetic"))
    return rows, commits, run, folder


def test_repo_trained_model_runs_without_lightgbm_and_matches_it(trained):
    rows, commits, run, folder = trained
    model = ScoringModel.load(folder_reader(folder), "repo", str(folder))
    X = build_features(rows.head(200), commits)
    expected = run.trained.model.predict(X)  # LightGBM + calibrator
    got = [model.predict(dict(zip(model.features, r, strict=True))) for r in X.to_numpy()]
    assert got == pytest.approx(expected, abs=1e-12)


@pytest.fixture(scope="module")
def result(trained):
    rows, commits, _, folder = trained
    model = ScoringModel.load(folder_reader(folder), "repo", "synthetic model")
    return bt.run_backtest(rows, commits, model), rows


def test_backtest_replays_exactly_the_held_out_commits(result):
    r, rows = result
    test, _ = bt.held_out_rows(rows)
    assert r.commits == test["commit"].nunique()
    assert r.sections == len(test)
    assert r.later_fixed == int(test["label"].sum())
    assert r.lines == int((test["added"] + test["removed"]).sum())
    assert r.repos == ["alpha", "beta"]


def test_backtest_totals_are_consistent(result):
    r, _ = result
    for name in bt.METHODS:
        t = r.totals[name]
        assert 0 <= t.caught <= r.later_fixed
        assert 0 <= t.lines_read <= r.lines
        assert r.binding[name].caught <= t.caught + 1e-9
    assert r.high_sections + r.medium_sections <= r.sections
    assert sum(c["later_fixed"] for c in r.per_repo_counts.values()) == r.later_fixed
    # Every section is eligible in the product plan, so it reads at least what the
    # gated version reads (D26).
    assert r.totals[bt.M_MODEL].caught >= r.totals[bt.M_GATED].caught


def test_replay_uses_the_same_section_picking_as_analyze(trained):
    _, _, _, folder = trained
    model = ScoringModel.load(folder_reader(folder), "repo", "m")
    import pandas as pd

    group = pd.DataFrame(
        {"label": [1, 0, 0], "added": [100, 3, 5], "removed": [0, 0, 1], "rule_score": [0, 0, 60]}
    )
    probs = [model.high_cut, 0.0, 0.0]  # the big one is high: must read
    totals, detail = bt.replay_commit(group, probs, model, ReadingBudget())
    cands = [
        Candidate(100, 0, RiskLevel.HIGH, probs[0], must=True),
        Candidate(3, 0, RiskLevel.LOW, 0.0),
        Candidate(6, 60, RiskLevel.HIGH, 0.0),
    ]
    expected, unfit = select_sections(cands, 40, 5, True)
    assert detail["model_pick"] == expected and expected[0] == 0
    assert totals[bt.M_MODEL].caught == 1
    assert totals[bt.M_SMALLEST].caught == 0  # 3 + 6 lines fit; the 100-line one doesn't


def test_examples_are_real_rows_with_fix_details(result):
    r, rows = result
    test, _ = bt.held_out_rows(rows)
    fixed = test[test["label"] == 1]
    keys = set(zip(fixed["commit"], fixed["path"], fixed["name"], strict=True))
    assert 0 < len(r.examples) <= bt.MAX_EXAMPLES
    for ex in [*r.examples, *r.missed]:
        assert (ex.commit, ex.path, ex.name) in keys
    assert all(ex.rank >= 1 for ex in r.examples)
    assert all(ex.rank == 0 for ex in r.missed)


def test_report_is_honest_about_what_it_measures(result):
    r, _ = result
    text = bt.render_report(r, ReadingBudget(), notes="## Summary\n\nHand-written.")
    for part in (
        "## Summary", "## Setup", "## Results: all held-out commits",
        "forces a choice", "## Per repo", 'How rare is "high"', "## Examples",
        "## Risk-coverage estimate", "## Limitations", "scope check (the main feature) is "
        "**not** part of this backtest", "days later in commit",
    ):  # fmt: skip
        assert part in text, part
    for name in bt.METHODS:
        assert name in text


def test_cli_backtest_on_a_dataset(trained, tmp_path):
    rows, _, _, folder = trained
    path = write_dataset(tmp_path, rows)
    report = tmp_path / "backtest.md"
    result = runner.invoke(
        app,
        ["backtest", "--dataset", str(path), "--model", str(folder), "--report", str(report),
         "--data-dir", str(tmp_path / "data")],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Replayed" in result.output and report.is_file()
    assert "Review Map plan (rules + model)" in report.read_text()


def test_cli_backtest_defaults_to_the_shipped_model(trained, tmp_path):
    rows, _, _, _ = trained
    path = write_dataset(tmp_path, rows)
    report = tmp_path / "b.md"
    result = runner.invoke(app, ["backtest", "--dataset", str(path), "--report", str(report)])
    assert result.exit_code == 0, result.output
    assert "shipped default model" in report.read_text()


@pytest.mark.parametrize("args", [[], ["--repo", ".", "--dataset", "x.parquet"]])
def test_cli_backtest_needs_exactly_one_source(args):
    result = runner.invoke(app, ["backtest", *args])
    assert result.exit_code != 0
    assert "exactly one of --repo PATH or --dataset FILE" in result.output


def test_cli_backtest_on_a_tiny_repo_says_why(git_repo, tmp_path):
    git_repo.write("a.py", "def f():\n    return 1\n")
    git_repo.commit("one")
    git_repo.write("a.py", "def f():\n    return 2\n")
    git_repo.commit("two")
    result = runner.invoke(
        app,
        ["backtest", "--repo", str(git_repo.path), "--data-dir", str(tmp_path / "d"),
         "--report", str(tmp_path / "b.md")],
    )  # fmt: skip
    assert result.exit_code != 0
    assert "Error:" in result.output and "Traceback" not in result.output

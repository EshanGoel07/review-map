"""Tests for Phase 10: features (no leakage), the time split, realistic labels,
training, calibration and evaluation.

The model tests use a synthetic dataset with a planted signal (rows touching a
"permissions" rule, or files with many earlier fixes, are riskier), so they can
check that the model learns something real without any git history.
"""

import json
import math
import sys

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from review_map.cli import app
from review_map.errors import ReviewMapError
from review_map.risk import model as model_mod
from review_map.risk.features import FEATURES, HISTORY_FEATURES, build_features, history_features
from review_map.risk.model import Calibrator, RiskModel
from review_map.training import evaluate as ev
from review_map.training import train as tr
from review_map.training.dataset import COLUMNS, commits_path

runner = CliRunner()


# --- Small hand-made tables --------------------------------------------------------


def commit_table(
    entries: list[tuple[str, str, str, bool, list[str]]], repo: str = "r"
) -> pd.DataFrame:
    """entries: (sha, ISO date, author, is_fix, paths), oldest first."""
    return pd.DataFrame(
        [
            {"repo": repo, "seq": i, "commit": sha, "date": pd.Timestamp(d, tz="UTC"),
             "author": a, "is_fix": fix, "paths": paths}
            for i, (sha, d, a, fix, paths) in enumerate(entries)
        ]
    )  # fmt: skip


def row_table(keys: list[tuple[str, str]], repo: str = "r") -> pd.DataFrame:
    return pd.DataFrame([{"repo": repo, "commit": c, "path": p} for c, p in keys])


HISTORY = [
    ("c1", "2020-01-01", "ann", False, ["app.py"]),
    ("c2", "2020-01-11", "bob", True, ["app.py", "tests/test_app.py"]),
    ("c3", "2020-01-21", "ann", False, ["app.py", "other.py"]),
]


# --- Features: history without leakage ------------------------------------------


def test_history_counts_only_earlier_commits():
    rows = row_table([("c1", "app.py"), ("c2", "app.py"), ("c3", "app.py"), ("c3", "other.py")])
    h = history_features(rows, commit_table(HISTORY))
    assert h.iloc[0].tolist() == [0.0, 0, 0, 0]  # first commit: no history, not even itself
    assert h.iloc[1].tolist() == [10.0, 1, 0, 1]  # c1 before it
    assert h.iloc[2].tolist() == [20.0, 2, 1, 2]  # c1 + the fix c2, two authors
    assert h.iloc[3].tolist() == [0.0, 0, 0, 0]  # other.py is new in c3


def test_leakage_future_commits_do_not_change_past_features():
    rows = row_table([("c2", "app.py"), ("c3", "app.py")])
    before = history_features(rows, commit_table(HISTORY))
    future = HISTORY + [
        ("c4", "2020-02-01", "cat", True, ["app.py"]),
        ("c5", "2020-03-01", "dan", True, ["app.py"]),
    ]
    after = history_features(rows, commit_table(future))
    pd.testing.assert_frame_equal(before, after)


def test_leakage_labels_never_feed_features():
    """Features must be identical whatever the labels say (labels depend on future fixes)."""
    rows = synthetic_rows(n_commits=60, seed=1)
    commits = synthetic_commits(rows)
    flipped = rows.copy()
    flipped["label"] = 1 - flipped["label"]
    flipped["days_to_fix"] = 5.0
    pd.testing.assert_frame_equal(build_features(rows, commits), build_features(flipped, commits))


def test_build_features_columns_and_rule_flags():
    rows = pd.DataFrame(
        [
            {"repo": "r", "commit": "c3", "path": "app.py", "added": 3, "removed": 1,
             "start_line": 10, "end_line": 19, "commit_files": 2, "commit_tests_changed": False,
             "is_fix": False, "kind": "method", "rule_score": 40,
             "rules": "auth,condition_loosened"},
        ]
    )  # fmt: skip
    X = build_features(rows, commit_table(HISTORY))
    assert list(X.columns) == FEATURES
    r = X.iloc[0]
    assert (r["lines_changed"], r["size"], r["kind_method"], r["kind_function"]) == (4, 10, 1, 0)
    assert (r["rule_auth"], r["rule_condition_loosened"], r["rule_crypto"]) == (1, 1, 0)
    assert r["past_changes"] == 2


# --- Synthetic dataset ---------------------------------------------------------------


def synthetic_rows(n_commits: int = 900, seed: int = 0, repos=("alpha", "beta")) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for repo in repos:
        start = pd.Timestamp("2015-01-01", tz="UTC")
        for i in range(n_commits):
            date = start + pd.Timedelta(days=int(i * 3))
            path = f"pkg/m{rng.integers(0, 12)}.py"
            for j in range(int(rng.integers(1, 4))):
                risky_rule = rng.random() < 0.2
                hot_file = path in ("pkg/m0.py", "pkg/m1.py")
                p = 0.03 + 0.25 * risky_rule + 0.10 * hot_file
                label = int(rng.random() < p)
                rows.append(
                    {
                        "repo": repo,
                        "commit": f"{repo}-{i:05d}",
                        "date": date,
                        "author": f"dev{rng.integers(0, 5)}",
                        "path": path,
                        "name": f"f{j}",
                        "kind": "function",
                        "start_line": 1,
                        "end_line": int(rng.integers(3, 60)),
                        "added": int(rng.integers(1, 30)),
                        "removed": int(rng.integers(0, 10)),
                        "commit_files": 1,
                        "commit_tests_changed": bool(rng.random() < 0.3),
                        "is_fix": bool(rng.random() < 0.1),
                        "rule_score": 25 if risky_rule else 0,
                        "rules": "permissions" if risky_rule else "",
                        "label": label,
                        "fix_commits": "x" if label else "",
                        "days_to_fix": float(rng.integers(5, 400)) if label else None,
                        "too_recent": i >= n_commits - 60,
                    }
                )
    return pd.DataFrame(rows, columns=COLUMNS)


def synthetic_commits(rows: pd.DataFrame) -> pd.DataFrame:
    out = []
    for repo, g in rows.groupby("repo", sort=False):
        firsts = g.drop_duplicates("commit").sort_values("date")
        for seq, r in enumerate(firsts.itertuples(index=False)):
            # Hot files get fix commits, so "past_fixes" carries the planted signal.
            out.append({"repo": repo, "seq": seq, "commit": r.commit, "date": r.date,
                        "author": r.author, "is_fix": r.path in ("pkg/m0.py", "pkg/m1.py"),
                        "paths": [r.path]})  # fmt: skip
    return pd.DataFrame(out)


# --- Time split and realistic labels -------------------------------------------------


def test_time_split_orders_by_date_keeps_commits_whole_and_drops_recent():
    rows = synthetic_rows(n_commits=300)
    split = tr.time_split(rows, embargo_days=0)
    parts = {k: rows.loc[getattr(split, k)] for k in ("train", "valid", "test")}
    for repo in ("alpha", "beta"):
        tr_, va, te = (p[p.repo == repo] for p in parts.values())
        assert tr_["date"].max() <= va["date"].min()
        assert va["date"].max() <= te["date"].min()
        assert split.test_start[repo] == te["date"].min()
    commits = [set(p["commit"]) for p in parts.values()]
    assert not (commits[0] & commits[1] or commits[1] & commits[2] or commits[0] & commits[2])
    assert not any(p["too_recent"].any() for p in parts.values())
    usable = (~rows["too_recent"]).sum()
    assert len(split.train) / usable == pytest.approx(0.70, abs=0.03)
    assert len(split.test) / usable == pytest.approx(0.15, abs=0.03)


def test_embargo_leaves_a_gap_before_the_test_period():
    rows = synthetic_rows(n_commits=600)
    split = tr.time_split(rows, embargo_days=180)
    for repo, start in split.test_start.items():
        r = rows[rows.repo == repo]
        used = r.loc[[i for i in split.train + split.valid if i in r.index]]
        assert used["date"].max() < start - pd.Timedelta(days=180)
        gap = r.loc[[i for i in split.embargoed if i in r.index]]
        assert len(gap) and gap["date"].min() >= start - pd.Timedelta(days=180)
        assert gap["date"].max() < start
    parts = [set(split.train), set(split.valid), set(split.test), set(split.embargoed)]
    assert sum(len(x) for x in parts) == len(set().union(*parts))  # disjoint
    assert len(split.train) / (len(split.train) + len(split.valid)) == pytest.approx(
        70 / 85, abs=0.03
    )


def test_embargo_zero_is_the_plain_split():
    rows = synthetic_rows(n_commits=300)
    split = tr.time_split(rows, embargo_days=0)
    assert split.embargoed == []


def test_known_labels_only_count_fixes_before_the_cutoff():
    rows = pd.DataFrame(
        {
            "repo": ["r", "r", "r", "s"],
            "date": pd.to_datetime(["2020-01-01"] * 4, utc=True),
            "label": [1, 1, 0, 1],
            "days_to_fix": [10.0, 100.0, None, 100.0],
        }
    )
    cutoff = {"r": pd.Timestamp("2020-02-01", tz="UTC")}  # 31 days later; no cutoff for "s"
    assert tr.known_labels(rows, cutoff).tolist() == [1, 0, 0, 1]


# --- Calibrator -----------------------------------------------------------------------


def test_calibrator_kinds():
    assert Calibrator("none").apply([0.2]) == [0.2]
    platt = Calibrator("platt", a=1.0, b=math.log(1 / 3))  # odds × 1/3
    assert platt.apply_one(0.5) == pytest.approx(0.25)
    iso = Calibrator("isotonic", xs=[0.1, 0.5], ys=[0.0, 0.4])
    assert iso.apply([0.0, 0.3, 0.9]) == pytest.approx([0.0, 0.2, 0.4])
    back = Calibrator.from_dict(json.loads(json.dumps(iso.to_dict())))
    assert back == iso


def test_fit_calibrator_fixes_overconfident_scores():
    rng = np.random.default_rng(3)
    true_p = rng.uniform(0.0, 0.3, 4000)
    y = (rng.random(4000) < true_p).astype(int)
    overconfident = np.clip(true_p * 2.5, 0, 0.99)
    cal, choice = tr.fit_calibrator(overconfident, y)
    assert choice["chosen"] in ("platt", "isotonic")
    fixed = cal.apply(overconfident)
    assert tr.brier(fixed, y) < tr.brier(overconfident, y)


def test_fit_calibrator_with_too_few_positives():
    cal, choice = tr.fit_calibrator([0.1, 0.2, 0.3], [0, 0, 1])
    assert cal.kind == "none"
    assert "too few" in choice["reason"]


# --- Metrics ------------------------------------------------------------------------------


def test_recall_at_effort():
    labels = [1, 0, 1, 0]
    effort = [10, 10, 10, 70]  # 100 lines in total
    perfect = [0.9, 0.1, 0.8, 0.0]
    assert ev.recall_at_effort(perfect, labels, effort, 0.20) == 1.0  # 20 lines: both risky
    assert ev.recall_at_effort(perfect, labels, effort, 0.10) == 0.5
    worst = [0.0, 0.9, 0.1, 0.8]
    assert ev.recall_at_effort(worst, labels, effort, 0.30, rule="prefix") == 0.0  # 70 lines block
    assert ev.recall_at_effort(worst, labels, effort, 0.30, rule="fit") == 1.0  # skipped instead
    tied = [0.5, 0.5, 0.5, 0.5]
    assert 0 < ev.recall_at_effort(tied, labels, effort, 0.20) < 1  # averaged over tie orders
    assert math.isnan(ev.recall_at_effort(perfect, [0, 0, 0, 0], effort, 0.2))


def test_fit_rule_skips_a_too_big_row_prefix_stops():
    scores = [0.9, 0.8, 0.7]
    effort = [5, 60, 5]  # budget 20% of 70 = 14 lines
    labels = [0, 0, 1]
    assert ev.recall_at_effort(scores, labels, effort, 0.20, rule="prefix") == 0.0
    assert ev.recall_at_effort(scores, labels, effort, 0.20, rule="fit") == 1.0


def test_hybrid_puts_high_rows_first_whatever_their_size():
    probs = np.array([0.50, 0.04, 0.03])
    lines = np.array([40.0, 1.0, 1.0])
    r = ev.model_rankings(probs, lines, high_cut=0.4)
    assert np.argsort(-r[ev.M_DENSITY]).tolist()[0] == 1  # per line: the tiny one first
    assert np.argsort(-r[ev.M_HYBRID]).tolist()[0] == 0  # hybrid: the big high-risk one first
    assert np.argsort(-r[ev.M_HYBRID]).tolist()[1:] == [1, 2]  # then per line


def test_hybrid_counts_high_rows_that_did_not_fit():
    rows = pd.DataFrame({"label": [1, 1, 0, 0], "added": [60, 60, 5, 5], "removed": [0, 0, 0, 0],
                         "rule_score": [0, 0, 0, 0]})  # fmt: skip
    probs = [0.9, 0.8, 0.01, 0.01]  # two big high-risk rows; 20% budget = 26 lines
    res = ev.compare(rows, probs, high_cut=0.5)
    assert res[ev.M_HYBRID]["high_rows"] == 2
    assert res[ev.M_HYBRID]["high_unfit_20"] == 2  # neither fits: reported, not hidden


def test_high_cut_and_ranking_come_from_validation_numbers():
    rng = np.random.default_rng(0)
    rows = pd.DataFrame({"label": rng.integers(0, 2, 200), "added": rng.integers(1, 30, 200),
                         "removed": 0, "rule_score": 0})  # fmt: skip
    probs = rng.random(200)
    chosen = ev.choose_high_cut(probs, rows)
    assert chosen["quantile"] in ev.HIGH_QUANTILES
    assert chosen["cut"] == pytest.approx(np.quantile(probs, chosen["quantile"]))
    best = {m: {"recall_20": 0.1} for m in ev.MODEL_METHODS} | {ev.M_DENSITY: {"recall_20": 0.5}}
    assert ev.choose_ranking(best) == ev.M_DENSITY


def test_calibration_measures_the_gap():
    rng = np.random.default_rng(5)
    p = rng.uniform(0, 0.4, 20000)
    y = (rng.random(20000) < p).astype(int)
    good = ev.calibration(p, y, base_rate=y.mean())
    assert good.ece < 0.01 and good.brier_skill > 0 and good.passed
    bad = ev.calibration(np.clip(p * 2, 0, 1), y, base_rate=y.mean())
    assert bad.ece > 0.05 and not bad.passed
    assert len(good.bins) == ev.CALIBRATION_BINS


def test_verdict_wording():
    base = {"pr_auc": 0.1, "roc_auc": 0.6, "recall_10": 0.1, "recall_20": 0.25, "recall_30": 0.3}
    m = ev.M_FUNCTION
    better = {m: base | {"recall_20": 0.40}, "random": base}
    worse = {m: base | {"recall_20": 0.10}, "rules (Phase 3)": base}
    tie = {m: base, "random": base}
    assert "beats the best baseline (random)" in ev.verdict(better, m)
    assert "does **not** beat the best baseline (rules (Phase 3))" in ev.verdict(worse, m)
    assert "ties" in ev.verdict(tie, m)
    # Other model rankings are never used as the "baseline" to beat.
    both = {m: base, ev.M_DENSITY: base | {"recall_20": 0.9}, "random": base}
    assert "(random)" in ev.verdict(both, m)


# --- Training end to end (needs LightGBM) -----------------------------------------


@pytest.fixture(scope="module")
def synthetic_run():
    rows = synthetic_rows()
    return ev.run_evaluation(rows, synthetic_commits(rows), holdout_repo="beta")


def test_model_learns_the_planted_signal(synthetic_run):
    results = synthetic_run.results
    model, random = results[ev.M_FUNCTION], results["random"]
    assert set(ev.MODEL_METHODS) <= set(results)
    assert synthetic_run.chosen_ranking in ev.MODEL_METHODS
    assert model["roc_auc"] > 0.65
    assert model["pr_auc"] > 1.5 * random["pr_auc"]
    names = [name for name, _ in synthetic_run.importances[:5]]
    assert "rule_permissions" in names or "rule_score" in names
    assert synthetic_run.cross_results is not None
    assert synthetic_run.cross_results[ev.M_FUNCTION]["roc_auc"] > 0.6


def test_calibrator_sees_validation_rows_only(monkeypatch):
    seen = []
    real = tr.fit_calibrator

    def spy(p, y, folds=5):
        seen.append(len(p))
        return real(p, y, folds)

    monkeypatch.setattr(tr, "fit_calibrator", spy)
    rows = synthetic_rows(n_commits=400)
    run = ev.run_evaluation(rows, synthetic_commits(rows))
    assert seen == [len(run.split.valid)]
    assert not set(run.split.test) & set(run.split.valid)


def test_report_and_model_card(synthetic_run):
    ablations = [("First results", ev.ablation_summary(synthetic_run))]
    repro = {"environment": ev.environment(), "dataset_meta": {"repos": {"alpha": {"head": "abc",
             "commits": 9}}}, "files": {"d.parquet": "f00"}}  # fmt: skip
    report = ev.render_report(
        synthetic_run, "## Summary\n\nHand-written summary.", ablations, repro
    )
    for text in (
        "# Review Map: model evaluation",
        "Hand-written summary.",
        "## Verdicts (test data)",
        "Ranking chosen on validation for Phase 11",
        "| model (per function)",
        "| model ÷ lines",
        "| model hybrid (high first, then ÷ lines)",
        "| rules (Phase 3) |",
        "| rules ÷ lines |",
        "| smallest change first |",
        "| largest change first |",
        "## Results on validation data",
        "| embargo gap (unused) |",
        "## Cross-repo: train on alpha, test on all of beta",
        "## Calibration",
        "ECE",
        "Top features by gain",
        "## Changes after first results",
        "made after the first test numbers were seen",
        "## Limitations and threats to validity",
        "## Reproducibility",
        "uv run review-map evaluate --holdout-repo click",
        "| lightgbm |",
        "`abc`",
        "reports/DECISIONS.md",
    ):
        assert text in report, text
    assert report.index("Hand-written summary.") < report.index("## Verdicts")
    assert "★" in report
    card = ev.model_card(synthetic_run, "synthetic")
    assert "Not a guarantee" in card
    assert "Calibration:" in card


def test_model_save_and_load_round_trip(synthetic_run, tmp_path):
    rows = synthetic_run.rows
    X = build_features(rows.head(50), synthetic_commits(rows))
    model = synthetic_run.trained.model
    model.metrics = ev.metrics_json(synthetic_run)
    model.save(tmp_path / "m", ev.model_card(synthetic_run, "synthetic"))
    loaded = RiskModel.load(tmp_path / "m")
    assert loaded.predict(X) == pytest.approx(model.predict(X))
    assert loaded.features == FEATURES
    assert json.loads((tmp_path / "m" / "metrics.json").read_text())["calibration"]["bins"]


def test_too_little_history_is_a_friendly_error():
    rows = synthetic_rows(n_commits=3, repos=("tiny",))
    rows["too_recent"] = False
    rows["label"] = 0
    with pytest.raises(ReviewMapError):
        ev.run_evaluation(rows, synthetic_commits(rows))


# --- CLI ----------------------------------------------------------------------------------


def write_dataset(tmp_path, rows):
    path = tmp_path / "dataset.parquet"
    rows.to_parquet(path, index=False)
    synthetic_commits(rows).to_parquet(commits_path(path), index=False)
    return path


def test_cli_evaluate(tmp_path):
    path = write_dataset(tmp_path, synthetic_rows(n_commits=500))
    notes = tmp_path / "notes.md"
    notes.write_text("## Discussion\n\nWhy the numbers look like this.\n")
    report = tmp_path / "reports" / "eval.md"
    result = runner.invoke(
        app,
        ["evaluate", "--dataset", str(path), "--holdout-repo", "beta", "--report", str(report),
         "--notes", str(notes), "--model-out", str(tmp_path / "model")],
        env={"COLUMNS": "200"},
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Calibration" in result.output
    text = report.read_text()
    assert "Why the numbers look like this." in text
    assert "## Changes after first results" in text
    assert text.count("| First results") + text.count("| + 180-day embargo") == 2
    assert (tmp_path / "model" / "model_card.md").is_file()
    metrics = json.loads((report.parent / "metrics.json").read_text())
    assert {
        "test",
        "validation",
        "cross_test",
        "calibration",
        "ablations",
        "reproducibility",
    } <= set(metrics)
    assert [a["selection"] for a in metrics["ablations"]] == ["prefix", "prefix", "fit"]
    assert [a["embargo_days"] for a in metrics["ablations"]] == [0, 180, 180]


def test_cli_evaluate_missing_or_old_dataset(tmp_path):
    result = runner.invoke(app, ["evaluate", "--dataset", str(tmp_path / "nope.parquet")])
    assert result.exit_code == 2
    assert "review-map mine" in result.output
    old = synthetic_rows(n_commits=50).drop(columns=["rule_score", "rules"])
    path = write_dataset(tmp_path, old)
    result = runner.invoke(app, ["evaluate", "--dataset", str(path)], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    assert "older version" in " ".join(result.output.split())


def test_cli_train_on_a_tiny_repo_says_why(git_repo, tmp_path):
    git_repo.write("app.py", "def f():\n    return 1\n")
    git_repo.commit("init")
    git_repo.write("app.py", "def f():\n    return 2\n")
    git_repo.commit("change f")
    result = runner.invoke(
        app, ["train", "--repo", str(git_repo.path), "--data-dir", str(tmp_path / "d")],
        env={"COLUMNS": "200"},
    )  # fmt: skip
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "Not enough history" in " ".join(result.output.split())


def test_cli_help():
    for command in ("evaluate", "train"):
        result = runner.invoke(app, [command, "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0


def test_missing_libomp_is_explained(monkeypatch):
    class Broken:
        def find_spec(self, name, path=None, target=None):
            if name == "lightgbm":
                raise OSError("dlopen(lib_lightgbm.dylib): Library not loaded: @rpath/libomp.dylib")
            return None

    monkeypatch.delitem(sys.modules, "lightgbm", raising=False)
    monkeypatch.setattr(sys, "meta_path", [Broken(), *sys.meta_path])
    with pytest.raises(ReviewMapError) as exc:
        model_mod.import_lightgbm()
    assert exc.value.hint == "On macOS: brew install libomp"


def test_history_feature_names_are_in_the_feature_list():
    assert set(HISTORY_FEATURES) <= set(FEATURES)

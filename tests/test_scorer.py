"""Tests for Phase 11: the pure-Python model reader, the scorer fallback chain, live
features, and how model levels shape the reading plan (no network)."""

import json
import shutil
from pathlib import Path

import pytest

from review_map.config import ReadingBudget
from review_map.errors import ReviewMapError
from review_map.models import ChangedFile, ChangedUnit, FileStatus, RiskLevel, UnitKind
from review_map.report.plan import (
    Candidate,
    all_reasons,
    build_plan,
    level_label,
    scorer_line,
    select_sections,
)
from review_map.report.render_json import render_json
from review_map.report.render_md import render_markdown
from review_map.risk import scorer as sc
from review_map.risk.features import FEATURES
from review_map.risk.lgb_text import ModelFormatError, TextBooster
from review_map.risk.scorer import (
    DEFAULT_MODEL_DIR,
    ChangeContext,
    ModelScore,
    ScoringModel,
    choose_model,
    commit_reader,
    file_history,
    folder_reader,
    unit_row,
)
from tests.test_plan import files_for, scored

# --- The text-model reader -------------------------------------------------------------

TINY = """tree
version=v4
num_class=1
num_tree_per_iteration=1
label_index=0
max_feature_idx=1
objective=binary sigmoid:1
feature_names=a b

Tree=0
num_leaves=3
num_cat=0
split_feature=0 1
split_gain=1 1
threshold=1.5 0.5
decision_type=2 10
left_child=-1 -2
right_child=1 -3
leaf_value=-1 0.5 2
leaf_weight=1 1 1
leaf_count=1 1 1
internal_value=0 0
internal_weight=0 0
internal_count=3 2
is_linear=0
shrinkage=1


Tree=1
num_leaves=1
num_cat=0
split_feature=
split_gain=
threshold=
decision_type=
left_child=
right_child=
leaf_value=0.25
leaf_weight=
leaf_count=
internal_value=
internal_weight=
internal_count=
is_linear=0
shrinkage=1


end of trees

parameters:
[objective: binary]
end of parameters
"""


def sigmoid(x: float) -> float:
    import math

    return 1 / (1 + math.exp(-x))


@pytest.mark.parametrize(
    ("x", "raw"),
    [
        ([1.0, 9.0], -1 + 0.25),  # a <= 1.5 → leaf 0
        ([2.0, 0.0], 0.5 + 0.25),  # b <= 0.5 → leaf 1
        ([2.0, 1.0], 2 + 0.25),  # b > 0.5 → leaf 2
        ([float("nan"), 0.0], -1 + 0.25),  # NaN without a NaN rule is read as 0
    ],
)
def test_text_booster_walks_trees(x, raw):
    booster = TextBooster.parse(TINY)
    assert booster.feature_names == ["a", "b"]
    assert booster.raw_score(x) == pytest.approx(raw)
    assert booster.predict(x) == pytest.approx(sigmoid(raw))


def test_text_booster_missing_values_go_the_default_way():
    # decision_type bits: 2 = default left; (1 << 2) = missing is zero; (2 << 2) = NaN.
    # With threshold -1, a plain comparison would send 0 right; "missing" sends it left.
    zero = TINY.replace("threshold=1.5 0.5", "threshold=1.5 -1").replace(
        "decision_type=2 10", "decision_type=2 6"
    )
    booster = TextBooster.parse(zero)
    assert booster.raw_score([2.0, 0.0]) == pytest.approx(0.5 + 0.25)  # missing → left
    assert booster.raw_score([2.0, -3.0]) == pytest.approx(0.5 + 0.25)  # -3 <= -1
    assert booster.raw_score([2.0, 3.0]) == pytest.approx(2 + 0.25)
    nan = TextBooster.parse(TINY.replace("threshold=1.5 0.5", "threshold=1.5 -1"))
    assert nan.raw_score([2.0, float("nan")]) == pytest.approx(0.5 + 0.25)  # NaN → left
    assert nan.raw_score([2.0, 0.0]) == pytest.approx(2 + 0.25)  # 0 > -1 → right


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("hello", "Not a LightGBM text model"),
        (TINY.replace("objective=binary sigmoid:1", "objective=regression"), "objective"),
        (TINY.replace("num_cat=0\nsplit_feature=0 1", "num_cat=1\nsplit_feature=0 1"), "Categ"),
        (TINY.replace("leaf_value=-1 0.5 2", "leaf_value=-1 0.5"), "Inconsistent"),
        (TINY.replace("split_feature=0 1", "split_feature=0 7"), "unknown feature"),
        (TINY.replace("leaf_value=-1 0.5 2", "leaf_value=-1 x 2"), "Malformed"),
    ],
)
def test_text_booster_rejects_what_it_cannot_run(text, message):
    with pytest.raises(ModelFormatError, match=message):
        TextBooster.parse(text)


def test_shipped_model_matches_lightgbm_exactly():
    lgb = pytest.importorskip("lightgbm")
    import numpy as np

    path = DEFAULT_MODEL_DIR / "model.txt"
    ours = TextBooster.parse(path.read_text())
    theirs = lgb.Booster(model_file=str(path))
    rng = np.random.default_rng(0)
    X = rng.random((300, len(FEATURES))) * rng.choice([1, 10, 100, 3000], len(FEATURES))
    X[:, 7:34] = (X[:, 7:34] > X[:, 7:34].mean()).astype(float)  # 0/1 flags
    expected = theirs.predict(X)
    got = [ours.predict(list(row)) for row in X]
    assert np.abs(np.array(got) - expected).max() < 1e-12


# --- Loading and the fallback chain --------------------------------------------------------


def test_shipped_model_loads_with_rare_levels():
    model = ScoringModel.load(folder_reader(DEFAULT_MODEL_DIR), "default", "x")
    assert model.features == FEATURES
    assert model.medium_cut < model.high_cut
    assert model.level(model.high_cut) is RiskLevel.HIGH
    assert model.level(model.medium_cut) is RiskLevel.MEDIUM
    assert model.level(0.0) is RiskLevel.LOW
    assert model.calibrated is False  # Phase 10: calibration not passed
    assert (DEFAULT_MODEL_DIR / "model_card.md").is_file()


@pytest.fixture
def repo_model(tmp_path) -> Path:
    folder = tmp_path / ".reviewmap" / "model"
    shutil.copytree(DEFAULT_MODEL_DIR, folder)
    return folder


def test_chain_prefers_the_repo_model(repo_model):
    choice = choose_model(folder_reader(repo_model), str(repo_model))
    assert choice.model is not None and choice.model.source == "repo"
    assert choice.model.label == "model trained on this repo"
    assert choice.notes == []


def test_chain_uses_the_default_model_when_the_repo_has_none(tmp_path):
    choice = choose_model(folder_reader(tmp_path / "nothing"), "nothing")
    assert choice.model is not None and choice.model.source == "default"
    assert choice.notes == []  # no model is the normal case: nothing to warn about


@pytest.mark.parametrize(
    ("file", "content"),
    [
        ("model.txt", "garbage"),
        ("features.json", "{not json"),
        ("features.json", '["added"]'),  # different features from the trees
        ("calibration.json", '{"kind": "magic"}'),
        ("metrics.json", "{}"),  # no level cuts
    ],
)
def test_corrupt_repo_model_falls_back_to_the_default(repo_model, file, content):
    (repo_model / file).write_text(content)
    choice = choose_model(folder_reader(repo_model), "my model")
    assert choice.model is not None and choice.model.source == "default"
    assert "my model" in choice.notes[0] and "shipped default" in choice.notes[0]


def test_repo_model_missing_a_file_falls_back(repo_model):
    (repo_model / "features.json").unlink()
    choice = choose_model(folder_reader(repo_model), "m")
    assert choice.model.source == "default"
    assert "missing features.json" in choice.notes[0]


def test_broken_default_model_falls_back_to_rules(tmp_path, monkeypatch):
    broken = tmp_path / "default"
    shutil.copytree(DEFAULT_MODEL_DIR, broken)
    (broken / "model.txt").write_text("nope")
    monkeypatch.setattr(sc, "DEFAULT_MODEL_DIR", broken)
    choice = choose_model(None, "")
    assert choice.model is None
    assert "Using the rules only" in choice.notes[0]


def test_rules_only_flag_skips_every_model(repo_model):
    choice = choose_model(folder_reader(repo_model), "m", use_model=False)
    assert choice.model is None
    assert "rules only" in choice.notes[0]


def test_commit_reader_reads_the_model_from_a_commit_not_the_tree(git_repo):
    git_repo.write("a.py", "x = 1\n")
    for f in DEFAULT_MODEL_DIR.iterdir():
        git_repo.write(f".reviewmap/model/{f.name}", f.read_text())
    base = git_repo.commit("base with model")
    (git_repo.path / ".reviewmap/model/model.txt").write_text("tampered in the working tree")
    reader = commit_reader(git_repo.path, base)
    assert reader("model.txt").startswith("tree")
    choice = choose_model(reader, "base")
    assert choice.model.source == "repo"


def test_pr_changes_read_the_repo_model_from_the_base(git_repo, monkeypatch):
    """A PR that adds its own model can't steer its own score."""
    from review_map import pipeline
    from review_map.config import Config as Cfg
    from review_map.config import LoadedConfig

    git_repo.write("a.py", "def f():\n    return 1\n")
    base = git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    for f in DEFAULT_MODEL_DIR.iterdir():
        git_repo.write(f".reviewmap/model/{f.name}", f.read_text())
    git_repo.write("a.py", "def f():\n    return 2\n")
    head = git_repo.commit("head adds a model")
    seen = []
    real = sc.choose_model
    monkeypatch.setattr(
        pipeline, "choose_model", lambda r, w, u=True: seen.append(w) or real(r, w, u)
    )
    change = pipeline.LocalChange(
        root=git_repo.path, base="main", head="feature", merge_base=base, head_sha=head,
        loaded=LoadedConfig(config=Cfg(), source=None), files=[], trusted_tree=False,
    )  # fmt: skip
    run = pipeline.plan_for(change, None, None, None, no_llm=True, no_cache=True)
    assert seen == [".reviewmap/model (base commit)"]
    assert run.plan.scorer == "shipped default model"  # the base has no model


# --- Live features -------------------------------------------------------------------------


def test_file_history_counts_earlier_commits(git_repo):
    git_repo.write("a.py", "x = 1\n")
    git_repo.commit("add a")
    git_repo.write("a.py", "x = 2\n")
    git_repo.commit("Fix the value of x")
    git_repo.write("b.py", "y = 1\n")
    base = git_repo.commit("add b")
    from datetime import UTC, datetime

    age, changes, fixes, authors = file_history(git_repo.path, base, "a.py", datetime.now(UTC))
    assert (changes, fixes, authors) == (2, 1, 1)
    assert 0 <= age < 1
    assert file_history(git_repo.path, base, "new.py", datetime.now(UTC)) == (0.0, 0, 0, 0)


def _unit(path="pkg/a.py", added=3, removed=1, kind=UnitKind.FUNCTION, start=10,
          language="python"):  # fmt: skip
    return ChangedUnit(
        path=path, language=language, name="f", kind=kind, start_line=start,
        end_line=None if start is None else start + 9,
        added_lines=list(range(added)), removed_lines=list(range(removed)),
    )  # fmt: skip


CTX = ChangeContext(
    commit_files=4, tests_changed=True, is_fix=False, history={"pkg/a.py": (100.0, 7, 2, 3)}
)


def test_unit_row_has_every_feature_like_training():
    s = scored("pkg/a.py", "f", 25)
    s.unit = _unit()
    row = unit_row(s, CTX)
    assert set(row) == set(FEATURES)
    assert row["added"] == 3 and row["removed"] == 1 and row["lines_changed"] == 4
    assert row["size"] == 10 and row["commit_files"] == 4 and row["commit_tests_changed"] == 1
    assert row["kind_function"] == 1 and row["kind_method"] == 0
    assert row["rule_score"] == 25
    assert (row["file_age_days"], row["past_changes"], row["past_fixes"]) == (100, 7, 2)
    assert row["past_authors"] == 3


@pytest.mark.parametrize(
    "unit",
    [
        _unit(path="tests/test_a.py"),  # tests: not in the training data
        _unit(path="docs/conf.py"),
        _unit(path="README.md", language=None),
        _unit(added=0),  # only removed lines
        _unit(start=None),  # deleted
    ],
)
def test_model_does_not_apply_outside_its_training_data(unit):
    s = scored("x", "f", 0)
    s.unit = unit
    assert unit_row(s, CTX) is None


# --- The reading plan with model levels -------------------------------------------------------


def C(lines, p=None, must=False, priority=0, level=RiskLevel.LOW):  # noqa: N802
    return Candidate(lines=lines, priority=priority, level=level, probability=p, must=must)


def test_must_read_sections_come_first_whatever_their_size():
    cands = [C(5, 0.05), C(500, 0.3, must=True), C(2, 0.01)]
    picked, unfit = select_sections(cands, budget_lines=40, max_units=5, use_model=True)
    assert picked[0] == 1  # the big high-risk one, although it's over the budget
    assert unfit == 0


def test_rest_is_filled_by_risk_per_line_skipping_what_does_not_fit():
    cands = [C(30, 0.06), C(5, 0.05), C(10, 0.04), C(20, 0.02)]
    # per line: 0.002, 0.01, 0.004, 0.001 → 1, 2, 0 (30 > 40-15 left? 15+30=45 > 40: skip), 3
    picked, _ = select_sections(cands, budget_lines=40, max_units=5, use_model=True)
    assert picked == [1, 2, 3]


def test_every_section_is_eligible_with_the_model_but_not_with_the_gate():
    cands = [C(5, 0.01, level=RiskLevel.LOW)]
    assert select_sections(cands, 40, 5, True)[0] == [0]  # D26
    assert select_sections(cands, 40, 5, True, gate=True)[0] == []  # before D26


def test_high_risk_sections_that_do_not_fit_are_counted():
    cands = [C(5, 0.2 + i / 100, must=True) for i in range(7)]
    picked, unfit = select_sections(cands, 40, max_units=5, use_model=True)
    assert picked == [6, 5, 4, 3, 2]  # most likely first
    assert unfit == 2


def test_units_the_model_does_not_score_keep_the_rules_gate():
    cands = [C(2, None, priority=0, level=RiskLevel.LOW), C(3, 0.001)]
    assert select_sections(cands, 40, 5, True)[0] == [1]  # e.g. changes.md, a test


def test_units_without_a_model_score_come_after_scored_ones():
    cands = [C(2, None, priority=50, level=RiskLevel.MEDIUM), C(30, 0.01)]
    picked, _ = select_sections(cands, 40, 5, True)
    assert picked == [1, 0]


def _model_plan(levels: dict[str, tuple[float, RiskLevel]], units, budget=None):
    scores = {f"{p}::{n}": ModelScore(prob, lvl) for (p, n), (prob, lvl) in levels.items()}
    return build_plan(
        units, files_for(units), budget or ReadingBudget(), base="m", head="h",
        model_scores=scores, scorer="shipped default model",
    )  # fmt: skip


def test_plan_says_how_many_high_risk_sections_did_not_fit():
    units = [scored(f"a{i}.py", "f", 0, lines=3) for i in range(7)]
    levels = {(f"a{i}.py", "f"): (0.3, RiskLevel.HIGH) for i in range(7)}
    plan = _model_plan(levels, units)
    assert plan.summary.read_units == 5
    assert plan.summary.high_risk_unfit == 2
    assert any("2 more high-risk sections didn't fit" in n for n in plan.notes)


def test_model_raises_the_level_and_adds_a_reason_next_to_rule_reasons():
    units = [scored("a.py", "f", 10), scored("b.py", "g", 0)]
    plan = _model_plan({("a.py", "f"): (0.1, RiskLevel.MEDIUM)}, units)
    item = next(i for i in plan.items if i.unit.name == "f")
    assert item.level is RiskLevel.MEDIUM and item.model_level is RiskLevel.MEDIUM
    assert level_label(item) == "medium (rules 10)"
    reasons = all_reasons(item)
    assert reasons[0].startswith("model: medium risk")
    assert "risk 10" in reasons  # the rule reason is still there


def test_probabilities_are_never_shown():
    units = [scored("a.py", "f", 10)]
    plan = _model_plan({("a.py", "f"): (0.123456, RiskLevel.HIGH)}, units)
    md, js = render_markdown(plan), render_json(plan)
    for text in (md, js):
        assert "0.123" not in text and "12.3" not in text and "12%" not in text
    data = json.loads(js)
    assert data["items"][0]["model_level"] == "high"
    assert data["risk_scorer"] == {"name": "shipped default model", "calibrated": False}
    assert "model_probability" not in js
    assert "not calibrated, so no percentages or risk-coverage estimate" in md


def test_scorer_line_wording_never_promises_safety():
    units = [scored("a.py", "f", 10)]
    lines = [
        scorer_line(_model_plan({}, units)),
        scorer_line(build_plan(units, files_for(units), ReadingBudget(), base="m", head="h")),
    ]
    assert lines[1] == "Risk: rules only."
    for line in lines:
        for word in ("safe", "no bugs", "guarantee", "bug-free"):
            assert word not in line.lower()


def test_rules_only_plan_is_unchanged_by_phase_11():
    units = [scored("a.py", "f", 40), scored("b.py", "g", 0)]
    plan = build_plan(units, files_for(units), ReadingBudget(), base="m", head="h")
    assert [i.unit.name for i in plan.read_carefully] == ["f"]
    assert plan.scorer == "rules" and all(i.model_level is None for i in plan.items)


# --- End to end on a real (tiny) repo ------------------------------------------------------


def test_analyze_uses_the_shipped_model_and_reads_levels(git_repo):
    from typer.testing import CliRunner

    from review_map.cli import app

    git_repo.write("pkg/core.py", "def f(x):\n    return x\n")
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.write("pkg/core.py", "def f(x):\n    if x:\n        return x\n    return 0\n")
    git_repo.commit("feature")
    result = CliRunner().invoke(
        app, ["analyze", "--repo", str(git_repo.path), "--base", "main", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["risk_scorer"]["name"] == "shipped default model"
    assert data["items"][0]["model_level"] in ("low", "medium", "high")
    assert data["items"][0]["rank"] == 1  # every section is eligible with the model


def test_unsupported_model_error_is_a_review_map_error():
    assert issubclass(ModelFormatError, ReviewMapError)


def test_changed_file_list_feeds_the_change_context(git_repo):
    git_repo.write("pkg/a.py", "x = 1\n")
    base = git_repo.commit("base")
    git_repo.write("pkg/a.py", "x = 2\n")
    git_repo.write("tests/test_a.py", "def test(): pass\n")
    head = git_repo.commit("Fix x")
    files = [
        ChangedFile(path="pkg/a.py", status=FileStatus.MODIFIED),
        ChangedFile(path="tests/test_a.py", status=FileStatus.ADDED),
    ]
    ctx = sc.change_context(git_repo.path, base, head, files)
    assert ctx.commit_files == 2 and ctx.tests_changed and ctx.is_fix
    assert ctx.history["pkg/a.py"][1] == 1  # one earlier change (the base commit)


BLOCKED = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"lightgbm", "numpy", "pandas", "sklearn", "pyarrow"}:
            raise ImportError(f"blocked: {name}")
sys.meta_path.insert(0, Block())
from review_map.cli import app
app(["analyze", "--repo", sys.argv[1], "--base", "main", "--no-llm", "--format", "json"])
"""


def test_analyze_with_the_model_needs_no_ml_libraries(git_repo):
    import subprocess
    import sys

    git_repo.write("pkg/core.py", "def f(x):\n    return x\n")
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.write("pkg/core.py", "def f(x):\n    return x + 1\n")
    git_repo.commit("feature")
    out = subprocess.run(
        [sys.executable, "-c", BLOCKED, str(git_repo.path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["risk_scorer"]["name"] == "shipped default model"

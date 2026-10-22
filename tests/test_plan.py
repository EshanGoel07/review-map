"""Tests for building the reading plan: boosts, budget, sections and the gate."""

import pytest

from review_map.config import ReadingBudget
from review_map.models import (
    MODULE_UNIT,
    ChangedFile,
    ChangedUnit,
    ClaimedScope,
    ClaimSource,
    FileStatus,
    MatchLevel,
    PlanSection,
    RiskLevel,
    RiskReason,
    RiskScore,
    ScoredUnit,
    UnitKind,
)
from review_map.report.plan import (
    BOOSTS,
    all_reasons,
    build_plan,
    gate_failed,
    line_range,
)
from review_map.risk.rules import level_for
from review_map.verify.scope import verify_scope


def scored(path: str, name: str, score: int, lines: int = 2, **unit_kwargs) -> ScoredUnit:
    kind = UnitKind.MODULE if name == MODULE_UNIT else UnitKind.FUNCTION
    unit = ChangedUnit(
        path=path,
        name=name,
        kind=kind,
        start_line=1,
        end_line=lines,
        added_lines=list(range(1, lines + 1)),
        **unit_kwargs,
    )
    reasons = [RiskReason(rule="r", points=score, message=f"risk {score}")] if score else []
    return ScoredUnit(
        unit=unit, risk=RiskScore(score=score, level=level_for(score), reasons=reasons)
    )


def files_for(units: list[ScoredUnit]) -> list[ChangedFile]:
    paths = dict.fromkeys(s.unit.path for s in units)
    return [ChangedFile(path=p, status=FileStatus.MODIFIED) for p in paths]


def plan_for(units, claims=None, budget=None, files=None):
    files = files or files_for(units)
    scope = verify_scope(claims, [s.unit for s in units], files) if claims else None
    return build_plan(
        units,
        files,
        budget or ReadingBudget(),
        base="main",
        head="HEAD",
        claims=claims,
        scope=scope,
        description="changes.md" if claims else None,
    )


def by_name(plan):
    return {i.unit.name: i for i in plan.items}


def llm_claims(**kwargs) -> ClaimedScope:
    return ClaimedScope(source=ClaimSource.LLM, **kwargs)


# --- Boosts ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "boost", "scope_level"),
    [
        (llm_claims(functions=["f"]), 0, MatchLevel.FUNCTION),
        (llm_claims(files=["a.py"]), 0, MatchLevel.FILE),
        (llm_claims(files=["src/"]), BOOSTS["folder_only"], MatchLevel.FOLDER),
        (llm_claims(functions=["other"]), BOOSTS["unclaimed"], MatchLevel.NONE),
        (llm_claims(functions=["other"], out_of_scope=["src/a.py"]), BOOSTS["out_of_scope"],
         MatchLevel.NONE),  # violation replaces the unclaimed boost, not on top
        (llm_claims(functions=["f"], out_of_scope=["src/a.py"]), BOOSTS["out_of_scope"],
         MatchLevel.FUNCTION),  # contradiction: claimed and out of scope
    ],
    ids=["function", "file", "folder", "unclaimed", "violation", "contradiction"],
)  # fmt: skip
def test_scope_boosts(claims, boost, scope_level):
    plan = plan_for([scored("src/a.py", "f", 40), scored("src/b.py", "other", 0)], claims)
    item = by_name(plan)["f"]
    assert item.boost == boost
    assert item.scope_level is scope_level
    assert item.priority == 40 + boost
    assert item.risk.score == 40  # the risk score itself never changes


def test_violation_reason_replaces_unclaimed_reason():
    plan = plan_for([scored("pay/r.py", "refund", 45)], llm_claims(out_of_scope=["pay/"]))
    item = plan.items[0]
    assert item.scope_reasons == ['out of scope: the description says "pay/"']
    assert item.out_of_scope_items == ["pay/"]
    assert item.is_unmentioned


def test_unclaimed_reason_uses_the_verifier_note():
    plan = plan_for(
        [scored("a.py", "f", 10), scored("a.py", "g", 10)],
        llm_claims(files=["a.py"], functions=["f"]),
    )
    reason = by_name(plan)["g"].scope_reasons[0]
    assert reason.startswith("unclaimed: a.py is mentioned, but this function is not")


def test_low_confidence_halves_boosts():
    claims = ClaimedScope(functions=["other"], low_confidence=True)
    plan = plan_for([scored("a.py", "f", 40), scored("b.py", "other", 0)], claims)
    assert by_name(plan)["f"].boost == BOOSTS["unclaimed"] // 2
    assert any("halved" in n for n in plan.notes)


def test_no_description_means_no_scope_and_a_note():
    plan = plan_for([scored("a.py", "f", 40)])
    item = plan.items[0]
    assert (item.boost, item.scope_level, item.scope_reasons) == (0, None, [])
    assert not item.is_unmentioned
    assert any("scope check was skipped" in n for n in plan.notes)


def test_priority_is_capped_at_100():
    plan = plan_for([scored("a.py", "f", 95)], llm_claims(functions=["x"]))
    assert plan.items[0].priority == 100
    assert plan.items[0].level is RiskLevel.HIGH


def test_unit_not_checked_by_verifier_gets_no_verdict():
    # e.g. the description file itself, which verify_scope leaves out.
    units = [scored("changes.md", MODULE_UNIT, 0), scored("a.py", "f", 40)]
    files = files_for(units)
    claims = llm_claims(functions=["f"])
    scope = verify_scope(claims, [s.unit for s in units], files, description_path="changes.md")
    plan = build_plan(units, files, ReadingBudget(), base="m", head="h", claims=claims, scope=scope)
    item = by_name(plan)[MODULE_UNIT]
    assert item.scope_level is None and item.boost == 0 and not item.is_unmentioned


def test_unmentioned_change_outranks_riskier_claimed_one():
    plan = plan_for(
        [scored("a.py", "claimed", 50), scored("b.py", "hidden", 40)],
        llm_claims(functions=["claimed"]),
    )
    assert [i.unit.name for i in plan.items] == ["hidden", "claimed"]
    assert [i.rank for i in plan.items] == [1, 2]


def test_ties_keep_risk_then_input_order():
    plan = plan_for([scored("a.py", "x", 30), scored("b.py", "y", 50), scored("c.py", "z", 30)])
    assert [i.unit.name for i in plan.items] == ["y", "x", "z"]


# --- Budget -------------------------------------------------------------------


def test_max_units():
    units = [scored(f"m{i}.py", f"f{i}", 60) for i in range(8)]
    plan = plan_for(units)
    assert plan.summary.read_units == 5
    assert [i.rank for i in plan.read_carefully] == [1, 2, 3, 4, 5]
    assert len(plan.skim) == 3


def test_low_priority_is_never_read_carefully():
    plan = plan_for([scored("a.py", "f", 29), scored("b.py", "g", 10)])
    assert plan.read_carefully == []
    assert any("Nothing stands out" in n for n in plan.notes)


def test_line_budget_uses_the_floor_for_small_changes():
    # 6 changed lines: 15% would be 1 line; the 40-line floor keeps both.
    plan = plan_for([scored("a.py", "f", 80, lines=3), scored("b.py", "g", 50, lines=3)])
    assert plan.summary.budget_lines == 40
    assert plan.summary.read_units == 2


def test_line_budget_uses_the_fraction_for_large_changes():
    units = [scored(f"m{i}.py", f"f{i}", 60, lines=100) for i in range(10)]  # 1000 lines
    plan = plan_for(units)
    assert plan.summary.budget_lines == 150
    assert plan.summary.read_units == 1  # 100 lines; another 100 would go over 150
    assert plan.summary.read_lines == 100


def test_top_unit_is_read_even_if_bigger_than_the_budget():
    plan = plan_for([scored("a.py", "huge", 90, lines=500)], budget=ReadingBudget(min_lines=10))
    assert plan.items[0].rank == 1


def test_smaller_unit_fills_remaining_budget():
    units = [
        scored("a.py", "first", 90, lines=30),
        scored("b.py", "too_big", 80, lines=30),  # 60 > 40: skipped
        scored("c.py", "small", 70, lines=5),  # 35 <= 40: fits
    ]
    plan = plan_for(units)
    assert [i.unit.name for i in plan.read_carefully] == ["first", "small"]
    assert by_name(plan)["too_big"].section is PlanSection.SKIM


def test_budget_from_config():
    units = [scored(f"m{i}.py", f"f{i}", 60) for i in range(4)]
    plan = plan_for(units, budget=ReadingBudget(max_units=2))
    assert plan.summary.read_units == 2
    assert plan.summary.max_units == 2


def test_summary():
    units = [scored("a.py", "f", 60, lines=3), scored("b.py", "g", 10, lines=4)]
    files = [*files_for(units), ChangedFile(path="x.lock", status=FileStatus.MODIFIED,
                                            generated_reason="lockfile")]  # fmt: skip
    plan = plan_for(units, files=files)
    s = plan.summary
    assert (s.files_changed, s.files_skipped, s.units_changed) == (3, 1, 2)
    assert (s.lines_changed, s.read_units, s.read_lines) == (7, 1, 3)
    assert plan.skipped_files == ["x.lock"]


# --- Gate ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scores", "fail_on", "failing"),
    [
        ([70, 10], "high", 1),
        ([50, 10], "high", 0),
        ([50, 10], "medium", 1),
        ([70, 40], "medium", 2),
        ([10], "medium", 0),
        ([70], None, 0),
    ],
)
def test_gate(scores, fail_on, failing):
    plan = plan_for([scored(f"m{i}.py", f"f{i}", s) for i, s in enumerate(scores)])
    assert len(gate_failed(plan, fail_on)) == failing


def test_gate_uses_priority_not_raw_risk():
    # Medium risk (45) + unclaimed (+20) = 65: high priority, so the gate fails.
    plan = plan_for([scored("a.py", "f", 45), scored("b.py", "g", 0)], llm_claims(functions=["g"]))
    assert by_name(plan)["f"].risk.level is RiskLevel.MEDIUM
    assert len(gate_failed(plan, "high")) == 1


# --- Helpers ------------------------------------------------------------------


def test_line_range_and_reasons():
    plan = plan_for([scored("a.py", "f", 40)], llm_claims(functions=["x"]))
    item = plan.items[0]
    assert line_range(item) == "1-2"
    assert all_reasons(item) == ["unclaimed: not mentioned in the description", "risk 40"]
    item.unit.start_line = None
    item.unit.old_start_line, item.unit.old_end_line = 3, 9
    assert line_range(item) == "old 3-9 (deleted)"


def test_highest_level():
    assert plan_for([scored("a.py", "f", 40)]).highest_level is RiskLevel.MEDIUM
    assert plan_for([]).highest_level is None

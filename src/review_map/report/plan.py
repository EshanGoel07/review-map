"""Combine risk scores and the scope report into a budgeted reading plan.

priority = risk score + scope boost (capped at 100):
- out-of-scope violation +30 (instead of the unclaimed boost, not on top of it)
- unclaimed change +20
- covered only by a folder claim +5
- boosts are halved when the claims came from the heuristic parser
- no description: no boosts, and a note that the scope check was skipped

"Read carefully" takes units from the highest priority down while both limits
hold: at most `max_units`, and at most max(lines_fraction × changed lines,
min_lines) changed lines. Only units with priority ≥ 30 (medium) qualify; the top
qualifying unit is always included, even if it's large. Everything else is "skim".
"""

from review_map.config import ReadingBudget
from review_map.models import (
    ChangedFile,
    ClaimedScope,
    MatchLevel,
    PlanItem,
    PlanSection,
    PlanSummary,
    ReadingPlan,
    RiskLevel,
    ScopeReport,
    ScoredUnit,
)
from review_map.risk.rules import level_for

BOOSTS = {"out_of_scope": 30, "unclaimed": 20, "folder_only": 5}
MIN_PRIORITY = 30  # below this (low), a unit is never "read carefully"

GATE_LEVELS = {"high": RiskLevel.HIGH, "medium": RiskLevel.MEDIUM}
_LEVEL_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


def _scope_for(
    scored: ScoredUnit, scope: ScopeReport, low_confidence: bool
) -> tuple[MatchLevel | None, list[str], list[str], int]:
    """(level, reasons, out-of-scope items, boost) for one unit."""
    unit = scored.unit
    coverage = next((c for c in scope.coverage if c.unit_id == unit.unit_id), None)
    if coverage is None:  # not checked by the verifier, e.g. the description file itself
        return None, [], [], 0
    level = coverage.level
    violated = [
        v.item for v in scope.out_of_scope_violations if (v.path, v.name) == (unit.path, unit.name)
    ]
    reasons: list[str] = []
    boost = 0
    if violated:
        boost = BOOSTS["out_of_scope"]
        reasons += [f'out of scope: the description says "{item}"' for item in violated]
    if violated:
        pass  # "out of scope" already says it; "unclaimed" too would be noise
    elif level is MatchLevel.NONE:
        boost = BOOSTS["unclaimed"]
        reasons.append(f"unclaimed: {coverage.note or 'not mentioned in the description'}")
    elif level is MatchLevel.FOLDER:
        boost = BOOSTS["folder_only"]
        reasons.append(f"only claimed by folder {coverage.claim}")
    if low_confidence:
        boost //= 2
    return level, reasons, violated, boost


def build_plan(
    scored: list[ScoredUnit],
    files: list[ChangedFile],
    budget: ReadingBudget,
    *,
    base: str,
    head: str,
    base_sha: str | None = None,
    head_sha: str | None = None,
    claims: ClaimedScope | None = None,
    scope: ScopeReport | None = None,
    description: str | None = None,
) -> ReadingPlan:
    notes: list[str] = []
    if claims is not None:
        notes += claims.notes
    if scope is not None:
        notes += scope.notes
    else:
        notes.append(
            "No description given, so the scope check was skipped. "
            "Pass --desc FILE or add changes.md to also find unmentioned changes."
        )
    low_confidence = bool(scope and scope.low_confidence)
    if low_confidence:
        notes.append("Scope boosts are halved because the claims are low confidence.")

    items: list[PlanItem] = []
    for s in scored:
        level, reasons, violated, boost = (
            _scope_for(s, scope, low_confidence) if scope else (None, [], [], 0)
        )
        priority = min(100, s.risk.score + boost)
        items.append(
            PlanItem(
                unit=s.unit,
                risk=s.risk,
                scope_level=level,
                scope_reasons=reasons,
                out_of_scope_items=violated,
                boost=boost,
                priority=priority,
                level=level_for(priority),
            )
        )
    # Highest priority first; ties: higher risk, then the scorer's order (file order).
    items.sort(key=lambda i: (-i.priority, -i.risk.score))

    lines_changed = sum(i.unit.lines_changed for i in items)
    budget_lines = max(round(budget.lines_fraction * lines_changed), budget.min_lines)
    used = 0
    rank = 0
    for item in items:
        if item.priority < MIN_PRIORITY or rank >= budget.max_units:
            break
        size = item.unit.lines_changed
        if rank and used + size > budget_lines:
            continue  # too big for what's left; a smaller, lower one may still fit
        rank += 1
        used += size
        item.section, item.rank = PlanSection.READ_CAREFULLY, rank

    if items and not rank:
        notes.append("Nothing stands out: no change reaches medium priority. Skim everything.")

    return ReadingPlan(
        base=base,
        head=head,
        base_sha=base_sha,
        head_sha=head_sha,
        description=description,
        claims=claims,
        scope=scope,
        items=items,
        summary=PlanSummary(
            files_changed=len(files),
            files_skipped=sum(not f.is_reviewable for f in files),
            units_changed=len(items),
            lines_changed=lines_changed,
            budget_lines=budget_lines,
            max_units=budget.max_units,
            read_units=rank,
            read_lines=used,
        ),
        skipped_files=[f.path for f in files if not f.is_reviewable],
        notes=notes,
    )


def gate_failed(plan: ReadingPlan, fail_on: str | None) -> list[PlanItem]:
    """Items at or above the `--fail-on` level (empty: the gate passes)."""
    if not fail_on:
        return []
    threshold = _LEVEL_ORDER[GATE_LEVELS[fail_on]]
    return [i for i in plan.items if _LEVEL_ORDER[i.level] >= threshold]


def line_range(item: PlanItem) -> str:
    """`11-14`, or `old 3-9 (deleted)` for code that only exists in the old version."""
    u = item.unit
    if u.start_line is not None:
        return f"{u.start_line}-{u.end_line}"
    return f"old {u.old_start_line}-{u.old_end_line} (deleted)"


def risk_reasons(item: PlanItem) -> list[str]:
    return [r.message for r in item.risk.reasons if r.points > 0]


def all_reasons(item: PlanItem) -> list[str]:
    """Scope reasons first (they explain the boost), then the risk reasons."""
    return [*item.scope_reasons, *risk_reasons(item)]

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

With a risk model (Phase 11, reports/DECISIONS.md D23-D26):
- a unit's level is the higher of its priority level and the model's level;
- "must read": units the model rates high, and out-of-scope violations. They come
  first, whatever their size, up to `max_units`; any that don't fit are counted and
  reported, never hidden;
- the rest of the budget is filled in order of model risk per changed line (the
  ranking chosen on validation in Phase 10), skipping any that don't fit. Every unit
  the model scores is eligible, low ones too (D26: the medium-or-higher gate read 23.9%
  of later-fixed functions in the backtest vs 60.6% without it). Units the model
  doesn't apply to (tests, docs, deleted code, the description file) keep the rules
  gate (medium or higher) and come after.
"""

from dataclasses import dataclass

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
from review_map.risk.scorer import ModelScore

BOOSTS = {"out_of_scope": 30, "unclaimed": 20, "folder_only": 5}
MIN_PRIORITY = 30  # below this (low), a unit is never "read carefully"

GATE_LEVELS = {"high": RiskLevel.HIGH, "medium": RiskLevel.MEDIUM}
_LEVEL_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


@dataclass
class Candidate:
    """What section picking needs to know about one unit (shared with the backtest)."""

    lines: int
    priority: int  # rule score + scope boost
    level: RiskLevel
    probability: float | None = None  # model probability; None: rules only / not applicable
    must: bool = False  # model "high" or out-of-scope violation: always read


def select_sections(
    cands: list[Candidate],
    budget_lines: int,
    max_units: int,
    use_model: bool,
    gate: bool = False,
) -> tuple[list[int], int]:
    """Indices of the units to read carefully, in reading order, and the number of
    must-read units that didn't fit `max_units`.

    Rules only: `cands` must already be in priority order (highest first).
    `gate=True` (model only) keeps the pre-D26 rule: only medium-or-higher units fill
    the budget. The backtest still measures it. Units without a model score always
    need medium or higher.
    """
    picked: list[int] = []
    used = 0
    if not use_model:
        for i, c in enumerate(cands):
            if c.priority < MIN_PRIORITY or len(picked) >= max_units:
                break
            if picked and used + c.lines > budget_lines:
                continue  # too big for what's left; a smaller, lower one may still fit
            picked.append(i)
            used += c.lines
        return picked, 0

    def density(i: int) -> tuple:
        c = cands[i]
        p = c.probability
        return (p is None, -(p or 0) / max(c.lines, 1), -c.priority, i)

    must = sorted(
        (i for i, c in enumerate(cands) if c.must),
        key=lambda i: (-(cands[i].probability or 0), -cands[i].priority, i),
    )
    for i in must[:max_units]:
        picked.append(i)
        used += cands[i].lines
    unfit = max(len(must) - max_units, 0)
    rest = sorted(
        (
            i
            for i, c in enumerate(cands)
            if not c.must
            and (
                (c.probability is not None and not gate)
                or _LEVEL_ORDER[c.level] >= _LEVEL_ORDER[RiskLevel.MEDIUM]
            )
        ),
        key=density,
    )
    for i in rest:
        if len(picked) >= max_units:
            break
        if picked and used + cands[i].lines > budget_lines:
            continue
        picked.append(i)
        used += cands[i].lines
    return picked, unfit


MODEL_REASONS = {
    RiskLevel.HIGH: "model: high risk (changes like this were among the 2% most often fixed later)",
    RiskLevel.MEDIUM: "model: medium risk (changes like this were among the 10% most often "
    "fixed later)",
}


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


NO_DESCRIPTION_NOTE = (
    "No description given, so the scope check was skipped. "
    "Pass --desc FILE or add changes.md to also find unmentioned changes."
)


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
    missing_description_note: str = NO_DESCRIPTION_NOTE,
    model_scores: dict[str, ModelScore] | None = None,
    scorer: str = "rules",
) -> ReadingPlan:
    notes: list[str] = []
    if claims is not None:
        notes += claims.notes
    if scope is not None:
        notes += scope.notes
    else:
        notes.append(missing_description_note)
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

    use_model = model_scores is not None
    if use_model:
        for item in items:
            ms = model_scores.get(item.unit_id)
            if ms is not None:
                item.model_level, item.model_probability = ms.level, ms.probability
                if _LEVEL_ORDER[ms.level] > _LEVEL_ORDER[item.level]:
                    item.level = ms.level

    lines_changed = sum(i.unit.lines_changed for i in items)
    budget_lines = max(round(budget.lines_fraction * lines_changed), budget.min_lines)
    cands = [
        Candidate(
            lines=i.unit.lines_changed,
            priority=i.priority,
            level=i.level,
            probability=i.model_probability,
            must=i.model_level is RiskLevel.HIGH or bool(i.out_of_scope_items),
        )
        for i in items
    ]
    picked, unfit = select_sections(cands, budget_lines, budget.max_units, use_model)
    for rank, idx in enumerate(picked, start=1):
        items[idx].section, items[idx].rank = PlanSection.READ_CAREFULLY, rank
    used = sum(items[i].unit.lines_changed for i in picked)
    rank = len(picked)
    if use_model:  # reading order first, then the rest by the same ordering
        order = {idx: n for n, idx in enumerate(picked)}
        reordered = sorted(range(len(items)), key=lambda i: order.get(i, len(order)))
        items = [items[i] for i in reordered]
    if unfit:
        notes.append(
            f"{unfit} more high-risk {'section' if unfit == 1 else 'sections'} didn't fit the "
            f"budget of {budget.max_units}: see the skim list, or raise reading_budget.max_units."
        )

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
            high_risk_unfit=unfit,
        ),
        skipped_files=[f.path for f in files if not f.is_reviewable],
        notes=notes,
        scorer=scorer,
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


def scorer_line(plan: ReadingPlan) -> str:
    """One line saying how risk was scored, and why no percentages are shown."""
    if plan.scorer == "rules":
        return "Risk: rules only."
    why = (
        "calibrated"
        if plan.scorer_calibrated
        else "not calibrated, so no percentages or risk-coverage estimate"
    )
    return f"Risk: rules + {plan.scorer} (shown as levels; {why})."


def level_label(item: PlanItem) -> str:
    """`75 high` (priority and level); `medium (rules 10)` when the model raised the level."""
    if item.model_level is not None and level_for(item.priority) is not item.level:
        return f"{item.level.value} (rules {item.priority})"
    return f"{item.priority} {item.level.value}"


def model_reason(item: PlanItem) -> str | None:
    return MODEL_REASONS.get(item.model_level) if item.model_level else None


def all_reasons(item: PlanItem) -> list[str]:
    """Scope reasons first (they explain the boost), then the model's level, then the
    rule reasons (they explain what in the code looks risky)."""
    model = model_reason(item)
    return [*item.scope_reasons, *([model] if model else []), *risk_reasons(item)]

"""Replay held-out history as if each change were a new PR (Phase 11).

For every commit in the test period (the newest 15% of each repo, which the model
never saw), build the reading plan exactly as `analyze` would: the same budget
(15% of changed lines, at least 40, at most 5 sections) and the same section
picking (`report.plan.select_sections`). Then check which of the commit's
functions later needed a bug fix (SZZ labels) and whether the plan said to read
them carefully.

Differences from a live `analyze` run, stated in the report:
- No description, so no scope check: history has no PR descriptions to verify.
- One commit = one "PR". Real PRs are often several commits.
- Features come from the mined dataset (computed leak-free in Phase 10), not git.

Compared with the same budget: the rules-only plan, "smallest change first" and
random picking (averaged over seeds).
"""

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from review_map.config import ReadingBudget
from review_map.models import RiskLevel
from review_map.report.plan import Candidate, select_sections
from review_map.risk.rules import level_for
from review_map.risk.scorer import ScoringModel

M_MODEL = "Review Map plan (rules + model)"
M_GATED = "same, medium-or-higher gate (before D26)"
M_RULES = "Review Map plan (rules only)"
M_DENSITY_ALL = "model ÷ lines only (no high-risk floor)"
M_SMALLEST = "smallest change first"
M_RANDOM = "random"
METHODS = (M_MODEL, M_GATED, M_RULES, M_DENSITY_ALL, M_SMALLEST, M_RANDOM)
RANDOM_SEEDS = 20
MAX_EXAMPLES = 5
_ORDER = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH]


@dataclass
class Totals:
    caught: float = 0.0
    lines_read: float = 0.0
    sections_read: float = 0.0

    def add(self, other: "Totals", weight: float = 1.0) -> None:
        self.caught += other.caught * weight
        self.lines_read += other.lines_read * weight
        self.sections_read += other.sections_read * weight


@dataclass
class Example:
    repo: str
    commit: str
    date: str
    path: str
    name: str
    rank: int
    model_level: str | None
    rules: str
    days_to_fix: float
    fix_commit: str
    fix_subject: str | None = None


@dataclass
class BacktestResult:
    commits: int = 0
    sections: int = 0
    lines: int = 0
    later_fixed: int = 0
    commits_with_fix: int = 0
    binding_commits: int = 0  # more changed lines than the budget, or > max_units sections
    totals: dict[str, Totals] = field(default_factory=lambda: {m: Totals() for m in METHODS})
    binding_later_fixed: int = 0
    binding_lines: int = 0
    binding: dict[str, Totals] = field(default_factory=lambda: {m: Totals() for m in METHODS})
    high_sections: int = 0
    high_later_fixed: int = 0
    high_commits: int = 0
    high_unfit: int = 0
    medium_sections: int = 0
    medium_later_fixed: int = 0
    examples: list[Example] = field(default_factory=list)
    missed: list[Example] = field(default_factory=list)
    per_repo: dict[str, dict[str, Totals]] = field(default_factory=dict)
    per_repo_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    model_source: str = ""
    repos: list[str] = field(default_factory=list)


def _plan(
    cands: list[Candidate], budget: ReadingBudget, use_model: bool, gate: bool = False
) -> tuple[list[int], int]:
    total = sum(c.lines for c in cands)
    budget_lines = max(round(budget.lines_fraction * total), budget.min_lines)
    if not use_model:  # the rules-only plan expects priority order
        order = sorted(range(len(cands)), key=lambda i: -cands[i].priority)
        picked, unfit = select_sections(
            [cands[i] for i in order], budget_lines, budget.max_units, False
        )
        return [order[i] for i in picked], unfit
    return select_sections(cands, budget_lines, budget.max_units, True, gate)


def _by_density(lines: list[int], scores: list[float], budget: ReadingBudget) -> list[int]:
    """Every section eligible, ordered by score per line, same budget rule."""
    cands = [
        Candidate(lines=n, priority=0, level=RiskLevel.MEDIUM, probability=s)
        for n, s in zip(lines, scores, strict=True)
    ]
    return _plan(cands, budget, True)[0]


def _totals(picked: list[int], labels: list[int], lines: list[int]) -> Totals:
    return Totals(
        caught=sum(labels[i] for i in picked),
        lines_read=sum(lines[i] for i in picked),
        sections_read=len(picked),
    )


def replay_commit(
    group: Any, probs: list[float], model: ScoringModel, budget: ReadingBudget
) -> tuple[dict[str, Totals], dict]:
    """The plans every method makes for one commit's rows, and details for the report."""
    labels = [int(v) for v in group["label"]]
    lines = [int(a + r) for a, r in zip(group["added"], group["removed"], strict=True)]
    rule_scores = [int(v) for v in group["rule_score"]]
    levels = [model.level(p) for p in probs]
    cands = []
    for n, score, p, m_level in zip(lines, rule_scores, probs, levels, strict=True):
        level = max(level_for(score), m_level, key=_ORDER.index)  # as in build_plan
        cands.append(
            Candidate(
                lines=n,
                priority=score,
                level=level,
                probability=p,
                must=m_level is RiskLevel.HIGH,
            )
        )
    model_pick, unfit = _plan(cands, budget, True)
    out = {
        M_MODEL: _totals(model_pick, labels, lines),
        M_GATED: _totals(_plan(cands, budget, True, gate=True)[0], labels, lines),
        M_RULES: _totals(_plan(cands, budget, False)[0], labels, lines),
        M_DENSITY_ALL: _totals(_by_density(lines, probs, budget), labels, lines),
        M_SMALLEST: _totals(_by_density(lines, [1.0] * len(lines), budget), labels, lines),
    }
    rand = Totals()
    for seed in range(RANDOM_SEEDS):
        rng = random.Random(seed)
        scores = [rng.random() * max(n, 1) for n in lines]  # ÷ lines below → uniform order
        rand.add(_totals(_by_density(lines, scores, budget), labels, lines), 1 / RANDOM_SEEDS)
    out[M_RANDOM] = rand
    detail = {
        "model_pick": model_pick,
        "unfit": unfit,
        "levels": levels,
        "labels": labels,
        "lines": lines,
        "binding": sum(lines) > max(round(budget.lines_fraction * sum(lines)), budget.min_lines)
        or len(lines) > budget.max_units,
    }
    return out, detail


def held_out_rows(rows: Any) -> tuple[Any, dict]:
    """The held-out rows (same split as training) and each repo's test start date."""
    from review_map.training.train import time_split

    rows = rows.reset_index(drop=True)
    split = time_split(rows)
    return rows.loc[split.test], split.test_start


def run_backtest(
    rows: Any,
    commits: Any,
    model: ScoringModel,
    budget: ReadingBudget | None = None,
) -> BacktestResult:
    from review_map.risk.features import build_features

    budget = budget or ReadingBudget()
    rows = rows.reset_index(drop=True)
    X = build_features(rows, commits)
    test, _ = held_out_rows(rows)
    result = BacktestResult(model_source=model.where, repos=sorted(test["repo"].unique()))
    feats = X.loc[test.index, model.features].to_numpy(dtype=float)
    probs_all = dict(
        zip(
            test.index,
            (model.calibrator.apply_one(model.booster.predict(list(x))) for x in feats),
            strict=True,
        )
    )
    candidates_ex: list[tuple] = []
    for (repo, sha), group in test.groupby(["repo", "commit"], sort=True):
        probs = [probs_all[i] for i in group.index]
        totals, d = replay_commit(group, probs, model, budget)
        n_fixed = sum(d["labels"])
        result.commits += 1
        result.sections += len(group)
        result.lines += sum(d["lines"])
        result.later_fixed += n_fixed
        result.commits_with_fix += n_fixed > 0
        repo_totals = result.per_repo.setdefault(repo, {m: Totals() for m in METHODS})
        counts = result.per_repo_counts.setdefault(repo, {"later_fixed": 0, "lines": 0})
        counts["later_fixed"] += n_fixed
        counts["lines"] += sum(d["lines"])
        for m in METHODS:
            result.totals[m].add(totals[m])
            repo_totals[m].add(totals[m])
            if d["binding"]:
                result.binding[m].add(totals[m])
        if d["binding"]:
            result.binding_commits += 1
            result.binding_later_fixed += n_fixed
            result.binding_lines += sum(d["lines"])
        highs = [i for i, lvl in enumerate(d["levels"]) if lvl is RiskLevel.HIGH]
        mediums = [i for i, lvl in enumerate(d["levels"]) if lvl is RiskLevel.MEDIUM]
        result.high_sections += len(highs)
        result.high_later_fixed += sum(d["labels"][i] for i in highs)
        result.high_commits += bool(highs)
        result.high_unfit += d["unfit"]
        result.medium_sections += len(mediums)
        result.medium_later_fixed += sum(d["labels"][i] for i in mediums)

        rows_list = list(group.itertuples(index=False))
        for idx, row in enumerate(rows_list):
            if not row.label:
                continue
            fix = (row.fix_commits or "").split(",")[0]
            rank = d["model_pick"].index(idx) + 1 if idx in d["model_pick"] else 0
            level = d["levels"][idx].value if d["levels"][idx] is not RiskLevel.LOW else None
            ex = Example(
                repo=repo,
                commit=sha,
                date=str(row.date)[:10],
                path=row.path,
                name=row.name,
                rank=rank,
                model_level=level,
                rules=row.rules or "",
                days_to_fix=float(row.days_to_fix or 0),
                fix_commit=fix,
            )
            candidates_ex.append((ex, d["binding"], len(rows_list)))
    result.examples, result.missed = pick_examples(candidates_ex)
    return result


def pick_examples(cands: list[tuple]) -> tuple[list[Example], list[Example]]:
    """Up to 5 caught examples (spread over repos, preferring commits where the budget
    mattered and fixes that came soon) and 2 missed ones, deterministically."""
    caught = [c for c in cands if c[0].rank]
    missed = [c for c in cands if not c[0].rank]

    def key(c: tuple) -> tuple:
        ex, binding, n = c
        return (not binding, ex.model_level != "high", ex.days_to_fix, ex.commit, ex.name)

    def spread(items: list[tuple], limit: int) -> list[Example]:
        out: list[Example] = []
        seen_repos: set[str] = set()
        seen_commits: set[str] = set()
        for pass_ in (0, 1):
            for c in sorted(items, key=key):
                ex = c[0]
                if len(out) >= limit or ex.commit in seen_commits:
                    continue
                if pass_ == 0 and ex.repo in seen_repos:
                    continue
                out.append(ex)
                seen_repos.add(ex.repo)
                seen_commits.add(ex.commit)
        return out

    return spread(caught, MAX_EXAMPLES), spread([c for c in missed if c[1]], 2)


def add_fix_subjects(result: BacktestResult, repo_dirs: dict[str, Path]) -> None:
    """Fill in the fix commits' subject lines from local clones, if they're there."""
    import subprocess

    for ex in [*result.examples, *result.missed]:
        repo = repo_dirs.get(ex.repo)
        if repo is None or not ex.fix_commit:
            continue
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%s", ex.fix_commit],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0:
            ex.fix_subject = out.stdout.strip()


# --- Report --------------------------------------------------------------------------


def _pct(x: float, total: float) -> str:
    return f"{100 * x / total:.1f}%" if total else "-"


def _table(totals: dict[str, Totals], later_fixed: int, lines: int, commits: int) -> list[str]:
    out = [
        "| Method | Later-fixed functions read | Changed lines read | Sections read per commit |",
        "|---|---:|---:|---:|",
    ]
    for m in METHODS:
        t = totals[m]
        per = t.sections_read / commits if commits else 0
        out.append(
            f"| {m} | {_pct(t.caught, later_fixed)} ({t.caught:.0f} of {later_fixed}) | "
            f"{_pct(t.lines_read, lines)} | {per:.1f} |"
        )
    return out


def _example_line(ex: Example) -> str:
    what = f"`{ex.path}` `{ex.name}` in {ex.repo} commit `{ex.commit[:10]}` ({ex.date})"
    flag = f"read carefully as #{ex.rank}" if ex.rank else '**not** in "read carefully"'
    model = f"model: {ex.model_level}" if ex.model_level else "model: low"
    rules = f"rules: {ex.rules.replace(',', ', ')}" if ex.rules else "no rule fired"
    subject = f' ("{ex.fix_subject}")' if ex.fix_subject else ""
    return (
        f"- {what}: {flag} ({model}; {rules}). Changed by a bug fix "
        f"{ex.days_to_fix:.0f} days later in commit `{ex.fix_commit[:10]}`{subject}."
    )


def render_report(r: BacktestResult, budget: ReadingBudget, notes: str | None = None) -> str:
    out = [
        "# Review Map: reading-plan backtest",
        "",
        "_Generated by `review-map backtest`. Model and evaluation details: "
        "`reports/eval.md`; methodology decisions: `reports/DECISIONS.md`._",
        "",
    ]
    if notes:
        out += [notes.strip(), ""]
    out += [
        "## Setup",
        "",
        f"- Replayed **{r.commits:,} commits** from the held-out test period of "
        f"{', '.join(r.repos)} (the newest 15% of each repo's history, never seen in "
        f"training): {r.sections:,} changed functions, {r.lines:,} changed lines, "
        f"**{r.later_fixed} later needed a bug fix** (in {r.commits_with_fix} commits).",
        f"- Each commit is treated as one PR and gets the real reading plan: "
        f"{budget.lines_fraction:.0%} of its changed lines (at least {budget.min_lines}), "
        f"at most {budget.max_units} sections, picked by the same code as `analyze`.",
        f'- Model: {r.model_source}. Levels: "high" = top 2% of validation scores, '
        '"medium" = top 10%.',
        "- No descriptions in history, so the scope check (the main feature) is **not** "
        "part of this backtest.",
        "",
        "## Results: all held-out commits",
        "",
        *_table(r.totals, r.later_fixed, r.lines, r.commits),
        "",
        f"Most commits are small: with the {budget.min_lines}-line floor the plan can read "
        "all of a small commit, so these numbers mostly measure the floor. The commits "
        "where the budget actually forces a choice are below.",
        "",
        f"## Results: commits where the budget forces a choice ({r.binding_commits:,})",
        "",
        "Commits with more changed lines than the budget, or more sections than "
        f"{budget.max_units}: {r.binding_later_fixed} later-fixed functions, "
        f"{r.binding_lines:,} changed lines.",
        "",
        *_table(r.binding, r.binding_later_fixed, r.binding_lines, r.binding_commits),
        "",
        "## Per repo (all held-out commits)",
        "",
        "| Repo | Later-fixed | " + " | ".join(METHODS) + " |",
        "|---|---:|" + "---:|" * len(METHODS),
    ]
    for repo in sorted(r.per_repo):
        c = r.per_repo_counts[repo]
        cells = [_pct(r.per_repo[repo][x].caught, c["later_fixed"]) for x in METHODS]
        out.append(f"| {repo} | {c['later_fixed']} | " + " | ".join(cells) + " |")
    out += [
        "",
        '## How rare is "high"?',
        "",
        f"- {r.high_sections} of {r.sections:,} changed functions "
        f"({_pct(r.high_sections, r.sections)}) were rated high, in {r.high_commits} of "
        f"{r.commits:,} commits; {_pct(r.high_later_fixed, r.high_sections)} of them later "
        f"needed a fix (all functions: {_pct(r.later_fixed, r.sections)}).",
        f"- Medium: {r.medium_sections} functions ({_pct(r.medium_sections, r.sections)}), "
        f"{_pct(r.medium_later_fixed, r.medium_sections)} later fixed.",
        f"- High sections that didn't fit the {budget.max_units}-section budget: "
        f'{r.high_unfit} (the plan reports these as "N more high-risk sections didn\'t fit").',
        "",
        "## Examples",
        "",
        "Caught: later-fixed functions the plan said to read carefully.",
        "",
        *[_example_line(ex) for ex in r.examples],
        "",
        "Missed: later-fixed functions the plan left to skim (from commits where the budget "
        "forced a choice).",
        "",
        *[_example_line(ex) for ex in r.missed],
        "",
        "## Risk-coverage estimate",
        "",
        "Not shown and not checked here: the model failed the Phase 10 calibration check "
        '(reports/eval.md), so Review Map shows no "the sections to read cover N% of the '
        'risk" estimate (reports/DECISIONS.md D27).',
        "",
        "## Limitations",
        "",
        '- SZZ labels are noisy (reports/eval.md). "Later fixed" is a proxy for "had a bug".',
        "- One commit is one PR here; real PRs often bundle several commits, which makes "
        "the budget bind more often.",
        "- No descriptions, so no scope boosts and no out-of-scope must-reads.",
        "- The test period is the same one used in reports/eval.md, so this is not a fresh "
        "holdout for decisions made after those results (D17-D23).",
        "",
    ]
    return "\n".join(out)

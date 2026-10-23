"""Evaluate the model against baselines, and check its calibration (Phase 10).

Metrics, always on rows the model didn't train on:
- PR-AUC (average precision): how well risky rows are ranked to the top. With ~7%
  risky rows, a random ranking scores about 0.07, not 0.5.
- ROC-AUC: chance a random risky row ranks above a random safe one (0.5 = random).
- Recall at X% of lines (the headline): pick rows in ranked order until X% of the
  changed lines are used up; what share of the risky rows were picked? Rows are
  picked with the reading plan's own budget rule (Phase 6): a row too big for the
  remaining budget is skipped and smaller ones can still fit ("fit"). The first
  results used "prefix" (stop at the first row that doesn't fit); see
  reports/DECISIONS.md. Ties are broken at random, averaged over TIE_SEEDS seeds.

Rankings compared:
- model (per function): the calibrated probability.
- model ÷ lines: probability per changed line, the usual ranking for a budget in lines.
- model hybrid: rows whose probability reaches the "high" cut come first (highest
  first) whatever their size; the rest follow by probability per line. The cut is
  chosen on validation data. High rows that don't fit are counted, not hidden.
- rules (Phase 3), rules ÷ lines ((score + 1) ÷ lines, so zero-score rows still
  order by size), largest change first, smallest change first, random.

Validation (never test) picks which model ranking Phase 11 uses.

Calibration (needed for Phase 11's risk-coverage estimate): a reliability table
over equal-count bins, the expected calibration error (ECE: the count-weighted
gap between predicted and observed rates), and the Brier score against always
predicting the training base rate.
"""

import hashlib
import importlib.metadata
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FRACTIONS = (0.10, 0.20, 0.30)
TIE_SEEDS = 10
RANDOM_SEEDS = 20
CALIBRATION_BINS = 10
ECE_TARGET = 0.02
HIGH_QUANTILES = (0.80, 0.90, 0.95, 0.98, 0.99)  # candidate "high" cuts, on validation scores
SELECTION = "fit"  # the reading plan's budget rule

M_FUNCTION = "model (per function)"
M_DENSITY = "model ÷ lines"
M_HYBRID = "model hybrid (high first, then ÷ lines)"
MODEL_METHODS = (M_FUNCTION, M_DENSITY, M_HYBRID)
R_RULES = "rules (Phase 3)"
R_DENSITY = "rules ÷ lines"
B_LARGEST = "largest change first"
B_SMALLEST = "smallest change first"
B_RANDOM = "random"


# --- Picking rows within a budget -----------------------------------------------


def select(scores: Any, effort: Any, fraction: float, seed: int, rule: str = SELECTION) -> Any:
    """Boolean mask of the rows read within `fraction` of the total lines."""
    import numpy as np

    s, e = np.asarray(scores, dtype=float), np.asarray(effort, dtype=float)
    budget = fraction * e.sum()
    order = np.lexsort((np.random.default_rng(seed).random(len(s)), -s))
    picked = np.zeros(len(s), dtype=bool)
    if rule == "prefix":
        picked[order[np.cumsum(e[order]) <= budget]] = True
        return picked
    used = 0.0
    for i in order:
        if used + e[i] <= budget:
            picked[i] = True
            used += e[i]
    return picked


def recall_at_effort(
    scores: Any, labels: Any, effort: Any, fraction: float, rule: str = SELECTION
) -> float:
    import numpy as np

    y = np.asarray(labels, dtype=float)
    if y.sum() == 0:
        return float("nan")
    hits = [y[select(scores, effort, fraction, seed, rule)].sum() for seed in range(TIE_SEEDS)]
    return float(np.mean(hits) / y.sum())


def score_metrics(
    scores: Any, labels: Any, effort: Any, rule: str = SELECTION, high: Any = None
) -> dict:
    import numpy as np
    from sklearn.metrics import average_precision_score, roc_auc_score

    y = np.asarray(labels, dtype=int)
    both = 0 < y.sum() < len(y)
    out = {
        "pr_auc": float(average_precision_score(y, scores)) if both else float("nan"),
        "roc_auc": float(roc_auc_score(y, scores)) if both else float("nan"),
    }
    for f in FRACTIONS:
        out[f"recall_{int(f * 100)}"] = recall_at_effort(scores, y, effort, f, rule)
    if high is not None:  # how many "high" rows didn't fit the budget
        high = np.asarray(high, dtype=bool)
        out["high_rows"] = int(high.sum())
        for f in FRACTIONS:
            unfit = [
                int((high & ~select(scores, effort, f, seed, rule)).sum())
                for seed in range(TIE_SEEDS)
            ]
            out[f"high_unfit_{int(f * 100)}"] = float(np.mean(unfit))
    return out


def _average(results: list[dict]) -> dict:
    return {k: sum(r[k] for r in results) / len(results) for k in results[0]}


def model_rankings(probs: Any, lines: Any, high_cut: float) -> dict[str, Any]:
    import numpy as np

    p, n = np.asarray(probs, dtype=float), np.maximum(np.asarray(lines, dtype=float), 1)
    density = p / n  # ≤ 1, so 2 + p always ranks above it
    return {M_FUNCTION: p, M_DENSITY: density, M_HYBRID: np.where(p >= high_cut, 2 + p, density)}


def compare(
    rows: Any, probs: Any | None, high_cut: float | None = None, rule: str = SELECTION
) -> dict[str, dict]:
    """Metrics for each model ranking (if `probs`) and every baseline, on the same rows."""
    import numpy as np

    y = rows["label"].to_numpy()
    lines = (rows["added"] + rows["removed"]).to_numpy().astype(float)
    results = {}
    if probs is not None:
        cut = high_cut if high_cut is not None else float("inf")
        for name, scores in model_rankings(probs, lines, cut).items():
            high = np.asarray(probs) >= cut if name == M_HYBRID else None
            results[name] = score_metrics(scores, y, lines, rule, high)
    rules = rows["rule_score"].to_numpy().astype(float)
    results[R_RULES] = score_metrics(rules, y, lines, rule)
    results[R_DENSITY] = score_metrics((rules + 1) / np.maximum(lines, 1), y, lines, rule)
    results[B_LARGEST] = score_metrics(lines, y, lines, rule)
    results[B_SMALLEST] = score_metrics(-lines, y, lines, rule)
    results[B_RANDOM] = _average(
        [
            score_metrics(np.random.default_rng(s).random(len(y)), y, lines, rule)
            for s in range(RANDOM_SEEDS)
        ]
    )
    return results


def choose_high_cut(probs: Any, rows: Any, rule: str = SELECTION) -> dict:
    """The hybrid's "high" cut: the validation-score quantile with the best validation
    recall at 20% of lines (ties → the higher cut, i.e. fewer forced rows)."""
    import numpy as np

    p = np.asarray(probs, dtype=float)
    y = rows["label"].to_numpy()
    lines = (rows["added"] + rows["removed"]).to_numpy().astype(float)
    tried = {}
    for q in HIGH_QUANTILES:
        cut = float(np.quantile(p, q)) if len(p) else float("inf")
        scores = model_rankings(p, lines, cut)[M_HYBRID]
        tried[q] = (cut, recall_at_effort(scores, y, lines, 0.20, rule))
    best_q = max(sorted(tried, reverse=True), key=lambda q: tried[q][1])
    return {
        "quantile": best_q,
        "cut": tried[best_q][0],
        "tried": {str(q): {"cut": c, "valid_recall_20": r} for q, (c, r) in tried.items()},
    }


def choose_ranking(valid_results: dict[str, dict]) -> str:
    """The model ranking with the best validation recall at 20% of lines."""
    return max(MODEL_METHODS, key=lambda m: valid_results[m]["recall_20"])


# --- Calibration ------------------------------------------------------------------------


@dataclass
class Calibration:
    bins: list[dict] = field(default_factory=list)  # n, predicted, observed
    ece: float = float("nan")
    brier: float = float("nan")
    brier_reference: float = float("nan")  # always predicting the training base rate
    passed: bool = False

    @property
    def brier_skill(self) -> float:
        if not self.brier_reference:
            return float("nan")
        return 1 - self.brier / self.brier_reference


def calibration(probs: Any, labels: Any, base_rate: float) -> Calibration:
    import numpy as np

    p, y = np.asarray(probs, dtype=float), np.asarray(labels, dtype=float)
    if not len(p):
        return Calibration()
    order = np.argsort(p, kind="stable")
    bins = []
    ece = 0.0
    for chunk in np.array_split(order, min(CALIBRATION_BINS, len(p))):
        if not len(chunk):
            continue
        predicted, observed = float(p[chunk].mean()), float(y[chunk].mean())
        bins.append({"n": int(len(chunk)), "predicted": predicted, "observed": observed})
        ece += len(chunk) / len(p) * abs(predicted - observed)
    result = Calibration(
        bins=bins,
        ece=float(ece),
        brier=float(np.mean((p - y) ** 2)),
        brier_reference=float(np.mean((base_rate - y) ** 2)),
    )
    result.passed = result.ece <= ECE_TARGET and result.brier_skill > 0
    return result


# --- A full evaluation run ----------------------------------------------------------


@dataclass
class EvalRun:
    rows: Any
    split: Any  # train.Split
    trained: Any  # train.Trained, the model that is saved
    results: dict[str, dict]  # pooled test set
    per_repo: dict[str, dict[str, dict]]
    valid_results: dict[str, dict]
    high: dict  # the hybrid's cut, chosen on validation
    chosen_ranking: str  # chosen on validation
    calibration: Calibration
    importances: list[tuple[str, float]]
    base_rate: float
    balance: dict  # split → rows, risky, rate
    embargo_days: int
    selection: str
    cross_repo: str | None = None
    cross_train: list[str] = field(default_factory=list)
    cross_results: dict[str, dict] | None = None
    cross_high: dict | None = None


def _with_labels(rows: Any, labels: Any) -> Any:
    out = rows.copy()
    out["label"] = labels.loc[rows.index].to_numpy()
    return out


def run_evaluation(
    rows: Any,
    commits: Any,
    holdout_repo: str | None = None,
    *,
    embargo_days: int | None = None,
    selection: str = SELECTION,
) -> EvalRun:
    """Time-split train/validate/test on every repo; optionally train on the others and
    test on `holdout_repo` (cross-repo)."""
    from review_map.errors import ReviewMapError
    from review_map.risk.features import build_features
    from review_map.training.train import EMBARGO_DAYS, known_labels, time_split, train_model

    embargo = EMBARGO_DAYS if embargo_days is None else embargo_days
    rows = rows.reset_index(drop=True)
    X = build_features(rows, commits)
    split = time_split(rows, embargo_days=embargo)
    if not len(split.test) or not len(split.valid) or not len(split.train):
        raise ReviewMapError(
            "Not enough history to split into train, validation and test periods.",
            hint="Mine a repo with a few hundred commits or more, spanning a year or more.",
        )
    y_known = known_labels(rows, split.test_start)
    trained = train_model(
        X.loc[split.train], y_known.loc[split.train], X.loc[split.valid], y_known.loc[split.valid]
    )
    valid = _with_labels(rows.loc[split.valid], y_known)
    p_valid = trained.model.predict(X.loc[split.valid])
    high = choose_high_cut(p_valid, valid, selection)
    valid_results = compare(valid, p_valid, high["cut"], selection)
    chosen = choose_ranking(valid_results)

    test = rows.loc[split.test]
    p_test = trained.model.predict(X.loc[split.test])
    results = compare(test, p_test, high["cut"], selection)
    per_repo = {}
    for repo in test["repo"].unique():
        mask = (test["repo"] == repo).to_numpy()
        probs = [p for p, m in zip(p_test, mask, strict=True) if m]
        per_repo[repo] = compare(test[mask], probs, high["cut"], selection)
    base_rate = float(y_known.loc[split.train].mean())
    cal = calibration(p_test, test["label"], base_rate)

    gains = trained.model.booster.feature_importance("gain")
    total = float(sum(gains)) or 1.0
    importances = sorted(
        ((name, float(g) / total) for name, g in zip(trained.model.features, gains, strict=True)),
        key=lambda kv: -kv[1],
    )
    balance = {}
    for name, idx, labels in (
        ("train", split.train, y_known),
        ("validation", split.valid, y_known),
        ("test", split.test, rows["label"]),
        ("embargo gap (unused)", split.embargoed, rows["label"]),
    ):
        risky = int(labels.loc[idx].sum()) if len(idx) else 0
        balance[name] = {"rows": len(idx), "risky": risky, "rate": risky / len(idx) if idx else 0}
    run = EvalRun(
        rows, split, trained, results, per_repo, valid_results, high, chosen, cal,
        importances, base_rate, balance, embargo, selection,
    )  # fmt: skip

    repos = list(rows["repo"].unique())
    if holdout_repo and holdout_repo in repos and len(repos) > 1:
        others = [r for r in repos if r != holdout_repo]
        tr = [i for i in split.train if rows.at[i, "repo"] != holdout_repo]
        va = [i for i in split.valid if rows.at[i, "repo"] != holdout_repo]
        cross = train_model(X.loc[tr], y_known.loc[tr], X.loc[va], y_known.loc[va])
        cross_high = choose_high_cut(
            cross.model.predict(X.loc[va]), _with_labels(rows.loc[va], y_known), selection
        )
        target = rows[(rows["repo"] == holdout_repo) & ~rows["too_recent"]]
        run.cross_repo, run.cross_train, run.cross_high = holdout_repo, others, cross_high
        run.cross_results = compare(
            target, cross.model.predict(X.loc[target.index]), cross_high["cut"], selection
        )
    return run


# --- Report ----------------------------------------------------------------------------

METRIC_HEADERS = [
    ("recall_10", "Recall @10% lines"),
    ("recall_20", "Recall @20% lines"),
    ("recall_30", "Recall @30% lines"),
    ("pr_auc", "PR-AUC"),
    ("roc_auc", "ROC-AUC"),
]


def _fmt(value: float, pct: bool = False) -> str:
    if value != value:  # NaN
        return "n/a"
    return f"{value:.1%}" if pct else f"{value:.3f}"


def metrics_table(results: dict[str, dict], mark: str | None = None) -> list[str]:
    """Markdown table; the best value per column in bold; `mark` gets a ★."""
    best = {
        key: max(r[key] for r in results.values() if r[key] == r[key]) for key, _ in METRIC_HEADERS
    }
    out = [
        "| Method | " + " | ".join(h for _, h in METRIC_HEADERS) + " |",
        "|---|" + "---:|" * len(METRIC_HEADERS),
    ]
    for name, r in results.items():
        cells = []
        for key, _ in METRIC_HEADERS:
            text = _fmt(r[key], pct=key.startswith("recall"))
            cells.append(f"**{text}**" if r[key] == best[key] else text)
        label = f"{name} ★" if name == mark else name
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    return out


def calibration_table(cal: Calibration) -> list[str]:
    out = ["| Bin | Rows | Predicted | Observed |", "|---:|---:|---:|---:|"]
    for i, b in enumerate(cal.bins, 1):
        out.append(f"| {i} | {b['n']} | {b['predicted']:.1%} | {b['observed']:.1%} |")
    return out


def verdict(results: dict[str, dict], method: str, key: str = "recall_20") -> str:
    """One honest sentence: does `method` beat the best non-model baseline on `key`?"""
    if method not in results:
        return "No model was trained."
    baselines = {k: v for k, v in results.items() if k not in MODEL_METHODS}
    name, best = max(baselines.items(), key=lambda kv: kv[1][key])
    mine = results[method][key]
    gap = mine - best[key]
    label = dict(METRIC_HEADERS)[key]
    pct = key.startswith("recall")
    a, b, g = _fmt(mine, pct), _fmt(best[key], pct), _fmt(abs(gap), pct)
    if gap > (0.01 if pct else 0.005):
        return f"{method} beats the best baseline ({name}) on {label}: {a} vs {b} (+{g})."
    if gap >= -(0.01 if pct else 0.005):
        return f"{method} ties the best baseline ({name}) on {label}: {a} vs {b}."
    return f"{method} does **not** beat the best baseline ({name}) on {label}: {a} vs {b} (−{g})."


def _data_section(run: EvalRun) -> list[str]:
    rows = run.rows
    out = [
        "| Repo | Rows | Risky | Rate | History | Test period from |",
        "|---|---:|---:|---:|---|---|",
    ]
    for repo, g in rows.groupby("repo", sort=False):
        start = run.split.test_start.get(repo)
        test_from = f"{start:%Y-%m-%d}" if start is not None else "n/a"
        out.append(
            f"| {repo} | {len(g):,} | {int(g['label'].sum()):,} | {g['label'].mean():.1%} | "
            f"{g['date'].min():%Y-%m} → {g['date'].max():%Y-%m} | {test_from} |"
        )
    out += ["", "| Split | Rows | Risky | Rate |", "|---|---:|---:|---:|"]
    for name, b in run.balance.items():
        out.append(f"| {name} | {b['rows']:,} | {b['risky']:,} | {b['rate']:.1%} |")
    out += [
        "",
        f"{int(rows['too_recent'].sum()):,} rows from each repo's last 180 days are left out "
        "everywhere (their outcome isn't known yet).",
    ]
    return out


LIMITATIONS = [
    "**Label noise (SZZ).** Bug fixes are found by commit-message keywords; some aren't bugs "
    "and some bugs are fixed without the words. Fixes that only add code (a missing check) "
    'blame nothing, so those bugs count as "safe". A fix may also touch lines that weren\'t '
    "the bug (refactors bundled with it).",
    '**Label 0 means "no fix found", not "correct".** Especially near the end of history.',
    "**Small data.** About 21,000 changed functions and 1,500 risky ones from three projects; "
    "the test periods hold a few thousand rows, so differences of a few points can be noise.",
    "**Python libraries only.** requests, flask and click: mature, well-tested Python "
    "libraries. Other languages, apps, or young codebases may behave differently.",
    "**Uneven history.** Most requests rows are from 2011-2013, so its test period starts in "
    "2017 while flask's and click's start in 2022-2023.",
    "**Per-commit rows, not per-PR.** Projects that squash or merge differently give different "
    "row sizes. The product scores PRs; the evaluation scores commits.",
    "**History per path.** Renames reset a file's history features.",
    "**Effort = changed lines.** Reading time isn't only line count; context and complexity "
    "matter too.",
    "**Choices made after seeing test results** (listed above and in reports/DECISIONS.md) "
    "can flatter the final numbers, even when each was justified on its own terms.",
]


def render_report(
    run: EvalRun,
    summary: str | None = None,
    ablations: list[tuple[str, dict]] | None = None,
    reproducibility: dict | None = None,
) -> str:
    info = run.trained.info
    cal = run.calibration
    chosen = run.chosen_ranking
    out = [
        "# Review Map: model evaluation",
        "",
        "_Generated by `review-map evaluate`; every number here is also in "
        "`reports/metrics.json`. Methodology decisions, with dates and whether they were made "
        "before or after seeing test results, are in `reports/DECISIONS.md`._",
    ]
    if summary:
        out += ["", summary.strip()]
    out += [
        "",
        "## Verdicts (test data)",
        "",
        f"- Ranking chosen on validation for Phase 11: **{chosen}**.",
        f"- {verdict(run.results, chosen, 'recall_20')}",
        f"- {verdict(run.results, M_FUNCTION, 'recall_20')}",
        f"- {verdict(run.results, M_FUNCTION, 'pr_auc')}",
    ]
    if run.cross_results is not None:
        out.append(
            f"- Cross-repo (train on {', '.join(run.cross_train)}, test on {run.cross_repo}): "
            + verdict(run.cross_results, chosen, "recall_20")
        )
    out.append(
        f"- Calibration: {'**passed**' if cal.passed else '**not passed**'} "
        f"(ECE {cal.ece:.3f}, target ≤ {ECE_TARGET}; Brier skill {cal.brier_skill:+.1%})."
    )
    out += [
        "",
        "## Data",
        "",
        *_data_section(run),
        "",
        "Labels: function-level SZZ (Phase 9). A row is risky if a later bug-fix commit changed "
        "a line it wrote. Split per repo by date, whole commits: newest 15% test; of rows at "
        f"least {run.embargo_days} days older than the test period, 70:15 train:validation; the "
        "gap between is unused (embargo), because those rows' fixes haven't had time to arrive. "
        "Training and validation only count fixes made before the test period starts.",
        "",
        "## Results on test data (newest 15% of each repo)",
        "",
        f'★ = the model ranking chosen on validation. Hybrid "high" cut: probability ≥ '
        f"{run.high['cut']:.3f} (the {run.high['quantile']:.0%} quantile of validation scores, "
        "chosen on validation).",
        "",
        *metrics_table(run.results, chosen),
        "",
        "Recall @X% lines: rows are picked in ranked order with the reading plan's budget rule "
        "until X% of changed lines are used; the share of later-fixed rows picked. Random picking "
        "gets about X%. Best value per column in bold.",
    ]
    hy = run.results.get(M_HYBRID)
    if hy:
        out += [
            "",
            f"Hybrid: {hy['high_rows']} high-risk rows in the test set; on average "
            f"{hy['high_unfit_10']:.1f} / {hy['high_unfit_20']:.1f} / {hy['high_unfit_30']:.1f} "
            "of them didn't fit the 10% / 20% / 30% budgets (the reading plan will say so).",
        ]
    for repo, results in run.per_repo.items():
        out += ["", f"### {repo} (test)", "", *metrics_table(results, chosen)]
    out += [
        "",
        "## Results on validation data",
        "",
        "Validation is the period just before the embargo gap. It chose the class weighting, "
        "the number of trees, the calibrator, the hybrid's cut and the ranking, so these numbers "
        "are optimistic for the model; they're shown because the choices were made on them.",
        "",
        *metrics_table(run.valid_results, chosen),
    ]
    if run.cross_results is not None:
        out += [
            "",
            f"## Cross-repo: train on {', '.join(run.cross_train)}, "
            f"test on all of {run.cross_repo}",
            "",
            f"Hybrid cut chosen on the training repos' validation data: "
            f"{run.cross_high['cut']:.3f}.",
            "",
            *metrics_table(run.cross_results, chosen),
        ]
    out += [
        "",
        "## Calibration (test data)",
        "",
        f"Calibrator: **{run.trained.calibration_choice.get('chosen', 'none')}**, chosen by "
        "cross-validated Brier score on validation data only "
        f"({run.trained.calibration_choice.get('cv_brier', run.trained.calibration_choice)}).",
        "",
        *calibration_table(cal),
        "",
        f"- ECE: **{cal.ece:.3f}** (target ≤ {ECE_TARGET}).",
        f"- Brier score: {cal.brier:.4f} vs {cal.brier_reference:.4f} for always predicting the "
        f"training base rate ({run.base_rate:.1%}): skill {cal.brier_skill:+.1%}.",
        f"- Verdict: {'passed' if cal.passed else 'not passed'}. Phase 11's risk-coverage "
        f"estimate is {'allowed' if cal.passed else 'not shown'} with this model.",
        "",
        "## Model",
        "",
        f"LightGBM, {info.best_iteration} trees (early stopping on validation PR-AUC), "
        f"scale_pos_weight {info.scale_pos_weight} (validation PR-AUC by weight: {info.tried}).",
        "",
        "Top features by gain:",
        "",
        "| Feature | Share of gain |",
        "|---|---:|",
        *[f"| `{name}` | {share:.1%} |" for name, share in run.importances[:12]],
    ]
    if ablations:
        out += [
            "",
            "## Changes after first results",
            "",
            "**These changes were made after the first test numbers were seen.** Each one is "
            "logged, with its reasoning, in `reports/DECISIONS.md`. The rows below are re-run "
            "by `review-map evaluate` every time, so the before/after is reproducible.",
            "",
            "| Configuration | Trees | Model per function: R@20% | PR-AUC | ROC-AUC | ECE | "
            "Brier skill | Best baseline R@20% |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for label, a in ablations:
            out.append(
                f"| {label} | {a['trees']} | {a['recall_20']:.1%} | {a['pr_auc']:.3f} | "
                f"{a['roc_auc']:.3f} | {a['ece']:.3f} | {a['brier_skill']:+.1%} | "
                f"{a['best_baseline']} {a['best_baseline_recall_20']:.1%} |"
            )
        out += [
            "",
            'New rankings added after the first results (no "before": they didn\'t exist): '
            f"{M_DENSITY}, {M_HYBRID}, {R_DENSITY}.",
        ]
    out += ["", "## Limitations and threats to validity", "", *[f"- {x}" for x in LIMITATIONS]]
    if reproducibility:
        out += ["", *reproducibility_section(reproducibility)]
    return "\n".join(out) + "\n"


def ablation_summary(run: EvalRun) -> dict:
    baselines = {k: v for k, v in run.results.items() if k not in MODEL_METHODS}
    name, best = max(baselines.items(), key=lambda kv: kv[1]["recall_20"])
    m = run.results[M_FUNCTION]
    return {
        "trees": run.trained.info.best_iteration,
        "recall_20": m["recall_20"],
        "pr_auc": m["pr_auc"],
        "roc_auc": m["roc_auc"],
        "ece": run.calibration.ece,
        "brier_skill": run.calibration.brier_skill,
        "best_baseline": name,
        "best_baseline_recall_20": best["recall_20"],
        "embargo_days": run.embargo_days,
        "selection": run.selection,
    }


def model_card(run: EvalRun, source: str) -> str:
    nan = float("nan")
    chosen = run.results.get(run.chosen_ranking, {})
    fn = run.results.get(M_FUNCTION, {})
    cal = run.calibration
    return "\n".join(
        [
            "# Review Map risk model",
            "",
            f"- Trained on: {source} ({', '.join(run.rows['repo'].unique())}), "
            f"{len(run.split.train):,} rows; validated on {len(run.split.valid):,}; "
            f"tested on the newest {len(run.split.test):,} "
            f"({run.embargo_days}-day embargo before the test period).",
            "- Predicts: the chance that a changed function will later be changed by a bug-fix "
            "commit (function-level SZZ labels, which are noisy).",
            f"- Ranking for reading plans (chosen on validation): {run.chosen_ranking}; "
            f'"high" means probability ≥ {run.high["cut"]:.3f}.',
            f"- Test: {run.chosen_ranking} recall @20% of lines "
            f"{_fmt(chosen.get('recall_20', nan), True)}; per function PR-AUC "
            f"{_fmt(fn.get('pr_auc', nan))}, ROC-AUC {_fmt(fn.get('roc_auc', nan))}.",
            f"- {verdict(run.results, run.chosen_ranking, 'recall_20')}",
            f"- Calibration: {'passed' if cal.passed else 'not passed'} (ECE {cal.ece:.3f}, "
            f"Brier skill {cal.brier_skill:+.1%}); calibrator "
            f"{run.trained.calibration_choice.get('chosen', 'none')}.",
            "- Limits: labels miss bugs fixed only by adding code, and may blame refactors bundled "
            "with fixes; history is per path (renames reset it); trained on Python projects.",
            "- Not a guarantee: a low score does not mean a change is correct.",
            "",
        ]
    )


def metrics_json(run: EvalRun) -> dict:
    info = run.trained.info
    return {
        "config": {"embargo_days": run.embargo_days, "selection": run.selection},
        "balance": run.balance,
        "test_start": {k: v.isoformat() for k, v in run.split.test_start.items()},
        "chosen_ranking": run.chosen_ranking,
        "high": run.high,
        "test": run.results,
        "test_per_repo": run.per_repo,
        "validation": run.valid_results,
        "cross_repo": run.cross_repo,
        "cross_train": run.cross_train,
        "cross_high": run.cross_high,
        "cross_test": run.cross_results,
        "calibration": {
            "passed": run.calibration.passed,
            "ece": run.calibration.ece,
            "brier": run.calibration.brier,
            "brier_reference": run.calibration.brier_reference,
            "brier_skill": run.calibration.brier_skill,
            "bins": run.calibration.bins,
            "calibrator": run.trained.calibration_choice,
        },
        "training": {
            "scale_pos_weight": info.scale_pos_weight,
            "best_iteration": info.best_iteration,
            "valid_pr_auc": info.valid_pr_auc,
            "tried": {str(k): v for k, v in info.tried.items()},
            "base_rate": run.base_rate,
        },
        "importances": [{"feature": f, "gain_share": g} for f, g in run.importances],
    }


# --- Reproducibility ----------------------------------------------------------------

PACKAGES = [
    "review-map",
    "lightgbm",
    "scikit-learn",
    "numpy",
    "pandas",
    "pyarrow",
    "pydriller",
    "tree-sitter",
    "tree-sitter-python",
    "unidiff",
]

COMMANDS = [
    "uv sync",
    "uv run review-map mine psf/requests pallets/flask pallets/click",
    "uv run review-map evaluate --holdout-repo click --notes reports/eval-notes.md",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def environment() -> dict:
    from review_map.training.train import PARAMS, SEED

    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    git = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "git": git.stdout.strip(),
        "packages": versions,
        "seeds": {
            "lightgbm": SEED,
            "calibration_cv": SEED,
            "tie_breaks": f"0..{TIE_SEEDS - 1}",
            "random_baseline": f"0..{RANDOM_SEEDS - 1}",
        },
        "lightgbm_params": PARAMS,
    }


def reproducibility_section(repro: dict) -> list[str]:
    env = repro["environment"]
    out = [
        "## Reproducibility",
        "",
        "Regenerate every number in this report:",
        "",
        "```bash",
        *COMMANDS,
        "```",
        "",
        "`mine` reuses existing clones in `data/repos/`. For the exact same data, check out the "
        "commits below in each clone first (e.g. `git -C data/repos/requests checkout <sha>`).",
        "",
        "| Repo | HEAD mined | Commits |",
        "|---|---|---:|",
    ]
    for name, meta in repro.get("dataset_meta", {}).get("repos", {}).items():
        out.append(f"| {name} | `{meta.get('head', '?')}` | {meta.get('commits', '?')} |")
    out += [
        "",
        "| File | SHA-256 |",
        "|---|---|",
        *[f"| `{name}` | `{digest}` |" for name, digest in repro.get("files", {}).items()],
        "",
        f"Python {env['python']} on {env['platform']}; {env['git']}.",
        "",
        "| Package | Version |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in env["packages"].items()],
        "",
        f"Seeds: {env['seeds']}. LightGBM runs single-threaded and deterministic.",
    ]
    return out

"""Train the LightGBM risk model with a time-based split (Phase 10).

Split, per repo, by commit date (never shuffled): the newest 15% of rows are only
used to test. Of the rows at least EMBARGO_DAYS older than the test period, the
oldest train the model and the rest (validation) pick the class weighting, stop
training early and fit the calibrator, in a 70:15 ratio. A commit's rows always
stay together. Rows in a repo's last RECENT_DAYS, and rows in the embargo gap,
are left out: whether they need a fix is not known yet.

Realistic labels: training and validation only count fixes that happened before
the test period starts, which is what someone training the model on that day
would know. Test rows use every known fix.
"""

from dataclasses import dataclass, field
from typing import Any

from review_map.errors import ReviewMapError
from review_map.risk.features import FEATURES
from review_map.risk.model import Calibrator, RiskModel, import_lightgbm

SPLIT = (0.70, 0.15, 0.15)
EMBARGO_DAYS = 180  # same horizon as the "too recent" rule (dataset.RECENT_DAYS)
SEED = 7

PARAMS = {
    "objective": "binary",
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_child_samples": 40,  # small leaves would memorise single commits
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "metric": "average_precision",
    "seed": SEED,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 1,  # reproducible results
    "verbose": -1,
}
MAX_ROUNDS = 1000
EARLY_STOP = 50


@dataclass
class Split:
    train: Any  # index labels of `rows`
    valid: Any
    test: Any
    test_start: dict  # repo → first test date (the "today" of the training run)
    embargoed: list = field(default_factory=list)  # rows in the gap, used nowhere


def time_split(
    rows: Any,
    fractions: tuple[float, float, float] = SPLIT,
    embargo_days: int = EMBARGO_DAYS,
) -> Split:
    """Per repo, whole commits at a time: newest share → test; of the commits at least
    `embargo_days` older than the test period, the oldest → train and the rest → valid.

    The embargo gap is left out entirely: rows just before the test period haven't had
    time for their fixes to arrive, so their labels would read "safe" when they're
    really "unknown". `embargo_days=0` gives the plain 70/15/15 split.
    """
    import pandas as pd

    usable = rows[~rows["too_recent"]]
    train, valid, test, gap = [], [], [], []
    test_start = {}
    train_share = fractions[0] / (fractions[0] + fractions[1])
    for repo, group in usable.groupby("repo", sort=False):
        per_commit = group.groupby("commit", sort=False).agg(
            date=("date", "min"), n=("date", "size")
        )
        per_commit = per_commit.sort_values(["date"], kind="stable")
        share = per_commit["n"].cumsum() / per_commit["n"].sum()
        is_test = share > fractions[0] + fractions[1]
        if not is_test.any():
            continue
        start = per_commit.loc[is_test, "date"].min()
        test_start[repo] = start
        before = per_commit[~is_test]
        mature = before[before["date"] < start - pd.Timedelta(days=embargo_days)]
        inner = mature["n"].cumsum() / max(mature["n"].sum(), 1)
        buckets = {
            "train": set(inner.index[inner <= train_share]),
            "valid": set(inner.index[inner > train_share]),
            "test": set(per_commit.index[is_test]),
            "gap": set(before.index.difference(mature.index)),
        }
        for name, target in (("train", train), ("valid", valid), ("test", test), ("gap", gap)):
            target.extend(group.index[group["commit"].isin(buckets[name])])
    return Split(train, valid, test, test_start, gap)


def known_labels(rows: Any, cutoff: dict) -> Any:
    """Labels as known at `cutoff[repo]`: a fix counts only if it happened before then."""
    import pandas as pd

    fixed_at = rows["date"] + pd.to_timedelta(rows["days_to_fix"].fillna(0), unit="D")
    limit = rows["repo"].map(cutoff)
    known = (rows["label"] == 1) & (fixed_at < limit)
    return known.where(limit.notna(), rows["label"] == 1).astype(int)


@dataclass
class TrainInfo:
    scale_pos_weight: float
    best_iteration: int
    valid_pr_auc: float
    tried: dict  # scale_pos_weight → validation PR-AUC


def fit_booster(X_train: Any, y_train: Any, X_valid: Any, y_valid: Any) -> tuple[Any, TrainInfo]:
    """Train with and without positive-class weighting; keep whichever validates better."""
    lgb = import_lightgbm()
    positives = int(y_train.sum())
    if positives == 0:
        raise ReviewMapError(
            "No risky changes in the training data, so there is nothing to learn.",
            hint="The repo needs a history of bug-fix commits. Try a repo with more history.",
        )
    weights = {1.0, round((len(y_train) - positives) / positives, 2)}
    best = None
    tried = {}
    for spw in sorted(weights):
        params = {**PARAMS, "scale_pos_weight": spw}
        train_set = lgb.Dataset(X_train, label=y_train, free_raw_data=False)
        valid_set = lgb.Dataset(X_valid, label=y_valid, reference=train_set)
        has_valid = int(y_valid.sum()) > 0
        booster = lgb.train(
            params,
            train_set,
            num_boost_round=MAX_ROUNDS if has_valid else 200,
            valid_sets=[valid_set] if has_valid else [],
            callbacks=[lgb.early_stopping(EARLY_STOP, verbose=False)] if has_valid else [],
        )
        score = booster.best_score["valid_0"]["average_precision"] if has_valid else 0.0
        tried[spw] = round(float(score), 4)
        if best is None or score > best[0]:
            best = (score, spw, booster)
    score, spw, booster = best
    iteration = booster.best_iteration or booster.current_iteration()
    return booster, TrainInfo(spw, int(iteration), round(float(score), 4), tried)


def brier(probs: Any, labels: Any) -> float:
    import numpy as np

    p, y = np.asarray(probs, dtype=float), np.asarray(labels, dtype=float)
    return float(np.mean((p - y) ** 2)) if len(p) else 0.0


def _fit_one(kind: str, p: Any, y: Any) -> Calibrator:
    import numpy as np

    if kind == "platt":
        from sklearn.linear_model import LogisticRegression

        logits = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
        lr = LogisticRegression(C=1e6).fit(logits.reshape(-1, 1), y)
        return Calibrator("platt", a=float(lr.coef_[0][0]), b=float(lr.intercept_[0]))
    if kind == "isotonic":
        from sklearn.isotonic import IsotonicRegression

        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(p, y)
        return Calibrator(
            "isotonic",
            xs=[float(x) for x in iso.X_thresholds_],
            ys=[float(v) for v in iso.y_thresholds_],
        )
    return Calibrator("none")


def fit_calibrator(p_valid: Any, y_valid: Any, folds: int = 5) -> tuple[Calibrator, dict]:
    """Pick none / Platt / isotonic by cross-validated Brier score *within* validation data.

    Only validation rows are ever passed in: the test set must not shape the model.
    """
    import numpy as np
    from sklearn.model_selection import StratifiedKFold

    p, y = np.asarray(p_valid, dtype=float), np.asarray(y_valid, dtype=int)
    if y.sum() < folds or (len(y) - y.sum()) < folds:
        return Calibrator("none"), {"reason": "too few validation positives to calibrate"}
    scores: dict[str, list[float]] = {"none": [], "platt": [], "isotonic": []}
    for fit_idx, eval_idx in StratifiedKFold(folds, shuffle=True, random_state=SEED).split(p, y):
        for kind in scores:
            cal = _fit_one(kind, p[fit_idx], y[fit_idx])
            scores[kind].append(brier(cal.apply(p[eval_idx]), y[eval_idx]))
    means = {k: round(float(np.mean(v)), 5) for k, v in scores.items()}
    kind = min(means, key=means.get)
    return _fit_one(kind, p, y), {"cv_brier": means, "chosen": kind}


@dataclass
class Trained:
    model: RiskModel
    info: TrainInfo
    calibration_choice: dict


def train_model(X_train: Any, y_train: Any, X_valid: Any, y_valid: Any) -> Trained:
    booster, info = fit_booster(X_train[FEATURES], y_train, X_valid[FEATURES], y_valid)
    raw_valid = booster.predict(X_valid[FEATURES], num_iteration=info.best_iteration)
    calibrator, choice = fit_calibrator(raw_valid, y_valid)
    model = RiskModel(booster, list(FEATURES), calibrator, {})
    return Trained(model, info, choice)

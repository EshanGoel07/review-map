"""Pick the best available scorer: repo model, default model, rules (Phase 11).

1. A model trained on this repo (`review-map train`), in `.reviewmap/model/`.
2. The model shipped with Review Map (`assets/default_model/`).
3. The Phase 3 rules alone.

A model that is missing, corrupt or in an unsupported format is skipped with a
note, never a crash. The rules always run too: their reasons explain every flag,
and the model adds one more signal ("changes like this were often fixed later").

The model's probabilities are not shown. Calibration failed in Phase 10, so the
plan shows a level (low / medium / high) and reasons instead. The levels come
from the model's own validation scores: "medium" is the top 10%, "high" the top
2% (reports/DECISIONS.md D25).
"""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from review_map.diff import git_source
from review_map.errors import ReviewMapError
from review_map.models import ChangedFile, RiskLevel, ScoredUnit
from review_map.risk.features import FEATURES, HISTORY_FEATURES, KINDS, RULES
from review_map.risk.lgb_text import TextBooster
from review_map.risk.model import (
    CALIBRATION_FILE,
    FEATURES_FILE,
    METRICS_FILE,
    MODEL_FILE,
    Calibrator,
)
from review_map.risk.rules import is_test_path
from review_map.training.mine import is_fix_message
from review_map.training.szz import is_labelable

REPO_MODEL_DIR = ".reviewmap/model"
DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent / "assets" / "default_model"
MEDIUM_QUANTILE, HIGH_QUANTILE = "0.9", "0.98"

Reader = Callable[[str], str | None]  # file name in the model folder → text, or None


@dataclass
class ScoringModel:
    """A saved model, ready to score live changes without LightGBM installed."""

    booster: TextBooster
    features: list[str]
    calibrator: Calibrator
    medium_cut: float
    high_cut: float
    calibrated: bool  # passed the Phase 10 calibration check
    source: str  # "repo" or "default"
    where: str  # for messages: the folder, or "<base commit>:.reviewmap/model"

    @classmethod
    def load(cls, read: Reader, source: str, where: str) -> "ScoringModel":
        texts = {name: read(name) for name in (MODEL_FILE, FEATURES_FILE, CALIBRATION_FILE)}
        missing = [name for name, text in texts.items() if text is None]
        if missing:
            raise ReviewMapError(f"The model in {where} is missing {', '.join(missing)}.")
        try:
            booster = TextBooster.parse(texts[MODEL_FILE])
            features = json.loads(texts[FEATURES_FILE])
            calibrator = Calibrator.from_dict(json.loads(texts[CALIBRATION_FILE]))
            metrics = json.loads(read(METRICS_FILE) or "{}")
            tried = metrics["high"]["tried"]
            medium, high = float(tried[MEDIUM_QUANTILE]["cut"]), float(tried[HIGH_QUANTILE]["cut"])
            calibrated = bool(metrics.get("calibration", {}).get("passed", False))
        except ReviewMapError as exc:
            raise ReviewMapError(f"The model in {where} can't be used: {exc.message}") from None
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ReviewMapError(f"The model in {where} can't be used: {exc!r}") from None
        if features != booster.feature_names or not set(features) <= set(FEATURES):
            raise ReviewMapError(f"The model in {where} expects different features.")
        if calibrator.kind not in ("none", "platt", "isotonic"):
            raise ReviewMapError(f"The model in {where} has an unknown calibrator.")
        return cls(booster, features, calibrator, medium, high, calibrated, source, where)

    def predict(self, row: dict[str, float]) -> float:
        return self.calibrator.apply_one(self.booster.predict([row[f] for f in self.features]))

    def level(self, p: float) -> RiskLevel:
        if p >= self.high_cut:
            return RiskLevel.HIGH
        if p >= self.medium_cut:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @property
    def label(self) -> str:
        return "model trained on this repo" if self.source == "repo" else "shipped default model"


def folder_reader(folder: Path) -> Reader:
    def read(name: str) -> str | None:
        try:
            return (folder / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    return read


def commit_reader(repo: Path, commit: str) -> Reader:
    """Read the repo model from a commit (the PR's base), never the PR's own files."""
    return lambda name: git_source.read_file(repo, commit, f"{REPO_MODEL_DIR}/{name}")


@dataclass
class ModelChoice:
    model: ScoringModel | None  # None: rules only
    notes: list[str] = field(default_factory=list)


def choose_model(
    repo_reader: Reader | None, repo_where: str, use_model: bool = True
) -> ModelChoice:
    """The fallback chain: repo model → shipped default → rules. Never raises."""
    if not use_model:
        return ModelChoice(None, ["Risk scorer: rules only (--rules-only)."])
    notes = []
    if repo_reader is not None and repo_reader(MODEL_FILE) is not None:
        try:
            return ModelChoice(ScoringModel.load(repo_reader, "repo", repo_where))
        except ReviewMapError as exc:
            notes.append(f"{exc.message} Using the shipped default model instead.")
    try:
        model = ScoringModel.load(folder_reader(DEFAULT_MODEL_DIR), "default", "the package")
    except ReviewMapError as exc:
        notes.append(f"{exc.message} Using the rules only.")
        return ModelChoice(None, notes)
    return ModelChoice(model, notes)


# --- Live features: the same columns as training (risk/features.py) ---------------------


@dataclass
class ChangeContext:
    """What the model needs to know about the change as a whole."""

    commit_files: int
    tests_changed: bool
    is_fix: bool
    history: dict[str, tuple[float, int, int, int]]  # path → HISTORY_FEATURES


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def file_history(repo: Path, before: str, path: str, when: datetime) -> tuple[float, int, int, int]:
    """(age in days, changes, fix commits, authors) of `path` up to and including `before`.

    Mirrors training: non-merge commits, no rename following, committer dates.
    """
    out = _git(
        repo, "log", "--no-merges", "--no-renames", "--full-history",
        "--format=%x00%ct%x1f%an%x1f%s", before, "--", path,
    )  # fmt: skip
    dates, authors, fixes = [], set(), 0
    for record in out.split("\x00")[1:]:
        parts = record.strip("\n").split("\x1f")
        if len(parts) < 3:
            continue
        dates.append(int(parts[0]))
        authors.add(parts[1])
        fixes += is_fix_message(parts[2])
    if not dates:
        return (0.0, 0, 0, 0)
    first = datetime.fromtimestamp(min(dates), UTC)
    age = max((when - first).total_seconds() / 86400, 0.0)
    return (age, len(dates), fixes, len(authors))


def change_context(
    repo: Path, base_sha: str, head_sha: str, files: list[ChangedFile]
) -> ChangeContext:
    stamp = _git(repo, "log", "-1", "--format=%ct%x1f%s", head_sha).strip().split("\x1f", 1)
    when = datetime.fromtimestamp(int(stamp[0]), UTC) if stamp[0].isdigit() else datetime.now(UTC)
    subject = stamp[1] if len(stamp) > 1 else ""
    return ChangeContext(
        commit_files=len(files),
        tests_changed=any(is_test_path(f.path) for f in files),
        is_fix=is_fix_message(subject),
        history={f.path: file_history(repo, base_sha, f.path, when) for f in files},
    )


def unit_row(scored: ScoredUnit, ctx: ChangeContext) -> dict[str, float] | None:
    """The model's input for one unit, or None if the model doesn't apply to it.

    Training only had rows for source code (not tests or docs) with added lines,
    so the model isn't used for anything else.
    """
    u = scored.unit
    if u.start_line is None or not u.added_lines or not is_labelable(u.path, u.language):
        return None
    fired = {r.rule for r in scored.risk.reasons}
    row = {
        "added": float(u.added),
        "removed": float(u.removed),
        "lines_changed": float(u.lines_changed),
        "size": float(max((u.end_line or u.start_line) - u.start_line + 1, 1)),
        "commit_files": float(ctx.commit_files),
        "commit_tests_changed": float(ctx.tests_changed),
        "is_fix": float(ctx.is_fix),
        "rule_score": float(scored.risk.score),
    }
    row |= {f"kind_{k}": float(u.kind.value == k) for k in KINDS}
    row |= {f"rule_{r}": float(r in fired) for r in RULES}
    history = ctx.history.get(u.path, (0.0, 0, 0, 0))
    row |= {name: float(v) for name, v in zip(HISTORY_FEATURES, history, strict=True)}
    return row


@dataclass
class ModelScore:
    probability: float  # internal only: used for ordering, never shown
    level: RiskLevel


def model_scores(
    model: ScoringModel, scored: list[ScoredUnit], ctx: ChangeContext
) -> dict[str, ModelScore]:
    """unit_id → model score, for the units the model applies to."""
    out = {}
    for s in scored:
        row = unit_row(s, ctx)
        if row is not None:
            p = model.predict(row)
            out[s.unit.unit_id] = ModelScore(p, model.level(p))
    return out

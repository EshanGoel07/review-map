"""Load and run the LightGBM risk model (Phase 10-11).

A saved model is a folder:
  model.txt         LightGBM's own text format (the trees)
  calibration.json  how to turn raw scores into honest probabilities (see Calibrator)
  features.json     the feature names, in the order the model expects
  metrics.json      the evaluation numbers, for the model card and Phase 11
  model_card.md     what it was trained on, how well it did, and its limits
"""

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from review_map.errors import ReviewMapError

MODEL_FILE = "model.txt"
CALIBRATION_FILE = "calibration.json"
FEATURES_FILE = "features.json"
METRICS_FILE = "metrics.json"
CARD_FILE = "model_card.md"


def import_lightgbm() -> Any:
    try:
        import lightgbm
    except ImportError:
        raise ReviewMapError(
            "LightGBM is not installed.",
            hint="Install the training extra: pip install 'review-map[train]'.",
        ) from None
    except OSError as exc:  # the package is there but its native library can't load
        hint = "On macOS: brew install libomp" if "libomp" in str(exc) else None
        raise ReviewMapError(f"LightGBM could not load: {exc}".splitlines()[0], hint=hint) from None
    return lightgbm


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


@dataclass
class Calibrator:
    """Maps a raw model probability to a calibrated one.

    - "none": unchanged.
    - "platt": sigmoid(a * logit(p) + b), two numbers fitted on validation data.
    - "isotonic": a monotone step curve through (xs, ys), interpolated linearly.
    """

    kind: str = "none"
    a: float = 1.0
    b: float = 0.0
    xs: list[float] = field(default_factory=list)
    ys: list[float] = field(default_factory=list)

    def apply_one(self, p: float) -> float:
        if self.kind == "platt":
            return 1 / (1 + math.exp(-(self.a * _logit(p) + self.b)))
        if self.kind == "isotonic" and self.xs:
            if p <= self.xs[0]:
                return self.ys[0]
            if p >= self.xs[-1]:
                return self.ys[-1]
            for i in range(1, len(self.xs)):
                if p <= self.xs[i]:
                    x0, x1, y0, y1 = self.xs[i - 1], self.xs[i], self.ys[i - 1], self.ys[i]
                    return y0 if x1 == x0 else y0 + (y1 - y0) * (p - x0) / (x1 - x0)
        return p

    def apply(self, probs: Any) -> list[float]:
        return [self.apply_one(float(p)) for p in probs]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Calibrator":
        return cls(**{k: data[k] for k in ("kind", "a", "b", "xs", "ys") if k in data})


@dataclass
class RiskModel:
    booster: Any
    features: list[str]
    calibrator: Calibrator
    metrics: dict

    @classmethod
    def load(cls, folder: Path) -> "RiskModel":
        lgb = import_lightgbm()
        try:
            features = json.loads((folder / FEATURES_FILE).read_text(encoding="utf-8"))
            calibrator = Calibrator.from_dict(
                json.loads((folder / CALIBRATION_FILE).read_text(encoding="utf-8"))
            )
            metrics_path = folder / METRICS_FILE
            metrics = json.loads(metrics_path.read_text()) if metrics_path.is_file() else {}
            booster = lgb.Booster(model_file=str(folder / MODEL_FILE))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ReviewMapError(f"Could not load the model in {folder}: {exc}") from None
        except Exception as exc:  # LightGBM raises its own error type for bad model files
            raise ReviewMapError(f"Could not load the model in {folder}: {exc}") from None
        return cls(booster, features, calibrator, metrics)

    def raw(self, matrix: Any) -> list[float]:
        return [float(p) for p in self.booster.predict(matrix[self.features])]

    def predict(self, matrix: Any) -> list[float]:
        """Calibrated chance that each row's change later needs a fix."""
        return self.calibrator.apply(self.raw(matrix))

    def save(self, folder: Path, card: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(folder / MODEL_FILE))
        (folder / FEATURES_FILE).write_text(json.dumps(self.features, indent=2), encoding="utf-8")
        (folder / CALIBRATION_FILE).write_text(
            json.dumps(self.calibrator.to_dict(), indent=2), encoding="utf-8"
        )
        (folder / METRICS_FILE).write_text(json.dumps(self.metrics, indent=2), encoding="utf-8")
        (folder / CARD_FILE).write_text(card, encoding="utf-8")

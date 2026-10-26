"""Run a LightGBM model from its text file, in plain Python (Phase 11).

LightGBM saves a model as text: a header, then one block per tree listing each
split (feature, threshold, which way missing values go) and each leaf's value.
Prediction is "walk every tree to a leaf, add the leaf values, apply the sigmoid".
Doing that here means people who only *use* the shipped model don't need
lightgbm, numpy or scikit-learn installed. Training still uses LightGBM itself.

Supported: binary models with numerical splits (what `review-map train` makes).
Anything else (categorical splits, other objectives) raises `ModelFormatError`,
and the scorer falls back to the next model or to the rules.
"""

import math
from dataclasses import dataclass

from review_map.errors import ReviewMapError

_ZERO = 1e-35  # LightGBM's kZeroThreshold
_DEFAULT_LEFT = 2  # decision_type bit 1
_MISSING_SHIFT = 2  # decision_type bits 2-3: 0 none, 1 zero, 2 NaN
_MISSING_ZERO, _MISSING_NAN = 1, 2


class ModelFormatError(ReviewMapError):
    """The text isn't a LightGBM model this reader can run."""


@dataclass
class _Tree:
    split_feature: list[int]
    threshold: list[float]
    decision_type: list[int]
    left: list[int]
    right: list[int]
    leaf_value: list[float]

    def predict(self, x: list[float]) -> float:
        if not self.split_feature:  # a single leaf
            return self.leaf_value[0]
        node = 0
        while node >= 0:
            value = x[self.split_feature[node]]
            dtype = self.decision_type[node]
            missing = (dtype >> _MISSING_SHIFT) & 3
            if math.isnan(value) and missing != _MISSING_NAN:
                value = 0.0
            if (missing == _MISSING_ZERO and abs(value) <= _ZERO) or (
                missing == _MISSING_NAN and math.isnan(value)
            ):
                go_left = bool(dtype & _DEFAULT_LEFT)
            else:
                go_left = value <= self.threshold[node]
            node = self.left[node] if go_left else self.right[node]
        return self.leaf_value[~node]  # leaves are stored as -(index + 1)


def _numbers(text: str, kind: type) -> list:
    return [kind(v) for v in text.split()] if text.strip() else []


@dataclass
class TextBooster:
    feature_names: list[str]
    trees: list[_Tree]
    sigmoid: float = 1.0
    average_output: bool = False

    @classmethod
    def parse(cls, text: str) -> "TextBooster":
        blocks = [b.strip() for b in text.replace("\r\n", "\n").split("\n\n") if b.strip()]
        if not blocks or blocks[0].splitlines()[0].strip() != "tree":
            raise ModelFormatError("Not a LightGBM text model (no 'tree' header).")
        header = _fields(blocks[0])
        objective = header.get("objective", "").split()
        if not objective or objective[0] != "binary":
            raise ModelFormatError(f"Unsupported objective: {header.get('objective')!r}.")
        sigmoid = 1.0
        for part in objective[1:]:
            if part.startswith("sigmoid:"):
                sigmoid = float(part.split(":", 1)[1])
        if int(header.get("num_class", "1")) != 1:
            raise ModelFormatError("Only single-output (binary) models are supported.")
        names = header.get("feature_names", "").split()
        trees = []
        for block in blocks[1:]:
            first = block.splitlines()[0]
            if first.startswith("end of trees"):
                break
            if not first.startswith("Tree="):
                continue
            f = _fields(block)
            if int(f.get("num_cat", "0")):
                raise ModelFormatError("Categorical splits are not supported.")
            try:
                tree = _Tree(
                    split_feature=_numbers(f.get("split_feature", ""), int),
                    threshold=_numbers(f.get("threshold", ""), float),
                    decision_type=_numbers(f.get("decision_type", ""), int),
                    left=_numbers(f.get("left_child", ""), int),
                    right=_numbers(f.get("right_child", ""), int),
                    leaf_value=_numbers(f["leaf_value"], float),
                )
            except (KeyError, ValueError) as exc:
                raise ModelFormatError(f"Malformed tree block {first!r}: {exc}") from None
            n = len(tree.split_feature)
            sizes = {len(tree.threshold), len(tree.decision_type), len(tree.left), len(tree.right)}
            if sizes != {n} or len(tree.leaf_value) != n + 1:
                raise ModelFormatError(f"Inconsistent tree block {first!r}.")
            if any(i >= len(names) for i in tree.split_feature):
                raise ModelFormatError(f"Tree block {first!r} uses an unknown feature.")
            trees.append(tree)
        if not trees:
            raise ModelFormatError("The model has no trees.")
        return cls(names, trees, sigmoid, "average_output" in header)

    def raw_score(self, x: list[float]) -> float:
        total = sum(t.predict(x) for t in self.trees)
        return total / len(self.trees) if self.average_output else total

    def predict(self, x: list[float]) -> float:
        """Probability for one row; `x` is in `feature_names` order."""
        return 1 / (1 + math.exp(-self.sigmoid * self.raw_score(x)))


def _fields(block: str) -> dict[str, str]:
    out = {}
    for line in block.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
        elif line.strip():
            out[line.strip()] = ""  # flags such as "average_output"
    return out

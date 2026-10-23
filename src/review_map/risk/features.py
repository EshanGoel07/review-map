"""Feature extraction for the ML risk model (Phase 10).

Each row of the dataset (one changed function in one commit) becomes a vector of
numbers. Three groups:

- the change itself: lines added/removed, function size, unit kind, files in the
  commit, whether tests changed, whether the commit is itself a fix;
- the Phase 3 rules: their total score and one 0/1 flag per rule;
- the file's history *before* this commit: age, earlier changes, earlier fix
  commits, distinct earlier authors.

No leakage: history is computed by walking the repo's commits in order and
reading each file's state *before* applying the current commit, so nothing
from the commit itself or from later commits can reach its features. "Earlier
fix commits" uses commit messages (known at the time), never labels (which
depend on future fixes).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from review_map.risk.rules import WEIGHTS

KINDS = ["function", "method", "class", "module"]
RULES = sorted(WEIGHTS)

CHANGE_FEATURES = [
    "added",
    "removed",
    "lines_changed",
    "size",
    "commit_files",
    "commit_tests_changed",
    "is_fix",
]
KIND_FEATURES = [f"kind_{k}" for k in KINDS]
RULE_FEATURES = ["rule_score", *[f"rule_{r}" for r in RULES]]
HISTORY_FEATURES = ["file_age_days", "past_changes", "past_fixes", "past_authors"]
FEATURES = [*CHANGE_FEATURES, *KIND_FEATURES, *RULE_FEATURES, *HISTORY_FEATURES]


@dataclass
class _FileHistory:
    first_seen: datetime | None = None
    changes: int = 0
    fixes: int = 0
    authors: set[str] = field(default_factory=set)


def history_features(rows: Any, commits: Any) -> Any:
    """Per row: the history of its file as of just before its commit.

    `rows` needs repo, commit, path; `commits` is the commit table (repo, seq,
    commit, date, author, is_fix, paths). Returns a frame aligned with `rows`.
    """
    import pandas as pd

    wanted: dict[tuple[str, str], set[str]] = defaultdict(set)  # (repo, commit) → paths
    for repo, commit, path in zip(rows["repo"], rows["commit"], rows["path"], strict=True):
        wanted[(repo, commit)].add(path)

    found: dict[tuple[str, str, str], tuple[float, int, int, int]] = {}
    for repo, group in commits.groupby("repo", sort=False):
        state: dict[str, _FileHistory] = defaultdict(_FileHistory)
        for c in group.sort_values("seq").itertuples(index=False):
            date = c.date.to_pydatetime()
            # 1. Read the state for this commit's rows *before* applying the commit.
            for path in wanted.get((repo, c.commit), ()):
                h = state[path]
                age = (date - h.first_seen).total_seconds() / 86400 if h.first_seen else 0.0
                found[(repo, c.commit, path)] = (max(age, 0.0), h.changes, h.fixes, len(h.authors))
            # 2. Then apply it.
            for path in c.paths:
                h = state[path]
                h.first_seen = h.first_seen or date
                h.changes += 1
                h.fixes += bool(c.is_fix)
                h.authors.add(c.author)

    values = [
        found.get((r, c, p), (0.0, 0, 0, 0))
        for r, c, p in zip(rows["repo"], rows["commit"], rows["path"], strict=True)
    ]
    return pd.DataFrame(values, columns=HISTORY_FEATURES, index=rows.index)


def build_features(rows: Any, commits: Any) -> Any:
    """The model's input matrix for `rows`, columns in FEATURES order."""
    import pandas as pd

    out = pd.DataFrame(index=rows.index)
    out["added"] = rows["added"].astype(float)
    out["removed"] = rows["removed"].astype(float)
    out["lines_changed"] = out["added"] + out["removed"]
    out["size"] = (rows["end_line"] - rows["start_line"] + 1).clip(lower=1).astype(float)
    out["commit_files"] = rows["commit_files"].astype(float)
    out["commit_tests_changed"] = rows["commit_tests_changed"].astype(float)
    out["is_fix"] = rows["is_fix"].astype(float)
    for kind in KINDS:
        out[f"kind_{kind}"] = (rows["kind"] == kind).astype(float)
    out["rule_score"] = rows["rule_score"].astype(float)
    fired = rows["rules"].fillna("").str.split(",")
    for rule in RULES:
        out[f"rule_{rule}"] = fired.map(lambda names, rule=rule: float(rule in names))
    out[HISTORY_FEATURES] = history_features(rows, commits).astype(float)
    return out[FEATURES]

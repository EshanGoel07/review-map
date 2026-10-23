"""Build the per-function training dataset (Phase 9).

One row per changed function per commit, in non-test source files, using the same
diff parsing and function mapping as `review-map analyze`, so a model trained on
it sees what the product sees.

Label: 1 if a later bug fix changed a line this commit wrote inside this function
(function-level SZZ, see szz.py), else 0.

`too_recent`: commits from the last RECENT_DAYS before the repo's newest commit
have had little time to be fixed, so their 0 means "unknown" more than "safe".
Rows are kept with the flag; Phase 10's time-based split decides what to do.

Resumable: each commit's rows and blame results are appended to a JSON-lines
checkpoint in the work folder as soon as they're computed. A rerun skips the
commits already there, then rebuilds the labels from the whole file.
"""

import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from review_map.code.functions import collect_units
from review_map.config import Config
from review_map.diff import git_source
from review_map.diff.parser import parse_diff
from review_map.errors import ReviewMapError
from review_map.models import ChangedFile, ChangedUnit
from review_map.risk.rules import is_test_path
from review_map.training.mine import TRAIN_EXTRA_HINT, CommitInfo, head_sha, list_commits
from review_map.training.szz import implicated_by_fix, is_labelable

RECENT_DAYS = 180
CHECKPOINT = "commits.jsonl"
IGNORE_REVS = "ignore-revs.txt"

COLUMNS = [
    "repo",
    "commit",
    "date",
    "author",
    "path",
    "name",
    "kind",
    "start_line",
    "end_line",
    "added",
    "removed",
    "commit_files",
    "commit_tests_changed",
    "is_fix",
    "label",
    "fix_commits",
    "days_to_fix",
    "too_recent",
]


def commit_change(repo: Path, c: CommitInfo) -> tuple[list[ChangedFile], list[ChangedUnit]]:
    """The commit's diff against its parent, and the units it touched."""
    assert c.parent is not None
    files = parse_diff(git_source.get_diff(repo, c.parent, c.sha), Config())
    units = collect_units(
        files,
        read_old=lambda p: git_source.read_file(repo, c.parent, p),
        read_new=lambda p: git_source.read_file(repo, c.sha, p),
    )
    return files, units


def _rows(c: CommitInfo, files: list[ChangedFile], units: list[ChangedUnit]) -> list[dict]:
    tests_changed = any(is_test_path(f.path) for f in files)
    rows = []
    for u in units:
        if u.start_line is None or not u.added_lines:
            continue  # deleted, or only lost lines: nothing of this commit left to blame
        if not is_labelable(u.path, u.language):
            continue
        rows.append(
            {
                "commit": c.sha,
                "date": c.date.isoformat(),
                "author": c.author,
                "path": u.path,
                "name": u.name,
                "kind": u.kind.value,
                "start_line": u.start_line,
                "end_line": u.end_line,
                "added": u.added,
                "removed": u.removed,
                "added_lines": u.added_lines,  # for the label join; not saved in the dataset
                "commit_files": c.files,
                "commit_tests_changed": tests_changed,
                "is_fix": c.is_fix,
            }
        )
    return rows


def read_checkpoint(path: Path) -> list[dict]:
    """Records written so far. A torn last line (crash mid-write) is ignored."""
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


@dataclass
class MineStats:
    commits: int = 0
    processed: int = 0  # computed in this run (the rest came from the checkpoint)
    skipped_huge: int = 0
    skipped_root: int = 0
    skipped_error: int = 0  # diff couldn't be read; the reason is kept in the checkpoint
    fixes: int = 0
    implicated_lines: int = 0
    blame_errors: int = 0  # files git blame failed on; should be ~0, else the labels are off
    unmatched_lines: int = 0  # blamed on a commit/line with no dataset row (e.g. a test file)
    notes: list[str] = field(default_factory=list)


def mine_commits(
    repo: Path,
    work_dir: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    stop_after: int | None = None,
) -> tuple[list[dict], list[CommitInfo], MineStats]:
    """Walk history, appending each commit's rows (and blame results for fixes) to the checkpoint.

    `stop_after` processes at most that many new commits (for tests of resuming).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    commits = list_commits(repo)
    stats = MineStats(commits=len(commits))

    ignore_revs = work_dir / IGNORE_REVS
    ignore_revs.write_text("".join(f"{c.sha}\n" for c in commits if c.is_huge), encoding="utf-8")

    checkpoint = work_dir / CHECKPOINT
    records = read_checkpoint(checkpoint)
    done = {r["sha"] for r in records}
    todo = [c for c in commits if c.sha not in done]
    if checkpoint.is_file() and checkpoint.stat().st_size:
        with checkpoint.open("rb") as fh:
            fh.seek(-1, 2)
            torn = fh.read(1) != b"\n"
        if torn:  # a crash mid-write: end that line so the next record starts clean
            with checkpoint.open("a", encoding="utf-8") as fh:
                fh.write("\n")
    with checkpoint.open("a", encoding="utf-8") as out:
        for i, c in enumerate(todo, 1):
            if stop_after is not None and stats.processed >= stop_after:
                break
            record: dict[str, Any] = {"sha": c.sha, "rows": [], "implicated": []}
            if c.parent is None:
                record["skipped"] = "root"
            elif c.is_huge:
                record["skipped"] = "huge"
            else:
                try:
                    files, units = commit_change(repo, c)
                except ReviewMapError as exc:  # one odd commit mustn't stop a long run
                    record["skipped"] = "error"
                    record["error"] = exc.message[:200]
                else:
                    record["rows"] = _rows(c, files, units)
                    if c.is_fix:
                        found, failures = implicated_by_fix(
                            repo, c.sha, c.parent, files, ignore_revs
                        )
                        record["implicated"] = [f.to_dict() for f in found]
                        record["blame_errors"] = failures
            out.write(json.dumps(record) + "\n")
            out.flush()
            records.append(record)
            stats.processed += 1
            if on_progress:
                on_progress(i, len(todo))
    return records, commits, stats


def label_rows(
    records: Iterable[dict], commits: list[CommitInfo], repo_name: str, stats: MineStats
) -> list[dict]:
    """Join blame results onto rows: a row is risky if a later fix changed a line it added."""
    info = {c.sha: c for c in commits}
    newest = max((c.date for c in commits), default=None)
    records = list(records)

    # (introducing commit, path) → {line: [fix shas]}
    blamed: dict[tuple[str, str], dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        stats.skipped_huge += r.get("skipped") == "huge"
        stats.skipped_root += r.get("skipped") == "root"
        stats.skipped_error += r.get("skipped") == "error"
        stats.blame_errors += r.get("blame_errors", 0)
        commit = info.get(r["sha"])
        stats.fixes += bool(commit and commit.is_fix and "skipped" not in r)
        for imp in r["implicated"]:
            blamed[(imp["introduced"], imp["path"])][imp["line"]].append(imp["fix"])
            stats.implicated_lines += 1

    rows = []
    matched_lines: set[tuple[str, str, int]] = set()
    for r in records:
        for row in r["rows"]:
            lines = blamed.get((row["commit"], row["path"]), {})
            fixes = sorted({f for ln in row["added_lines"] if ln in lines for f in lines[ln]})
            matched_lines |= {
                (row["commit"], row["path"], ln) for ln in row["added_lines"] if ln in lines
            }
            date = datetime.fromisoformat(row["date"])
            fix_dates = [info[f].date for f in fixes if f in info]
            out = {k: v for k, v in row.items() if k != "added_lines"}
            out |= {
                "repo": repo_name,
                "label": int(bool(fixes)),
                "fix_commits": ",".join(fixes),
                "days_to_fix": (min(fix_dates) - date).total_seconds() / 86400
                if fix_dates
                else None,
                "too_recent": bool(newest and date > newest - timedelta(days=RECENT_DAYS)),
            }
            rows.append(out)
    stats.unmatched_lines = sum(
        1 for key, lines in blamed.items() for ln in lines if (*key, ln) not in matched_lines
    )
    return rows


def to_frame(rows: list[dict]) -> Any:
    try:
        import pandas as pd
    except ImportError:
        raise ReviewMapError("pandas is not installed.", hint=TRAIN_EXTRA_HINT) from None
    frame = pd.DataFrame(rows, columns=COLUMNS)
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    return frame


def save(frame: Any, path: Path) -> None:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        raise ReviewMapError("pyarrow is not installed.", hint=TRAIN_EXTRA_HINT) from None
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def build_dataset(
    repo: Path,
    work_dir: Path,
    repo_name: str,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    stop_after: int | None = None,
) -> tuple[list[dict], MineStats, str]:
    """Mine one repo and label its rows. Returns rows, stats and the HEAD it was mined at."""
    records, commits, stats = mine_commits(
        repo, work_dir, on_progress=on_progress, stop_after=stop_after
    )
    rows = label_rows(records, commits, repo_name, stats)
    return rows, stats, head_sha(repo)

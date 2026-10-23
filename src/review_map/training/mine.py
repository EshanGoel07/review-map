"""Clone repos and walk their history with PyDriller (Phase 9).

A commit counts as a **bug fix** when its subject line (first line of the message)
has one of the fix words below, and none of the "not really a fix" words. Only
the subject is used: bodies often say "fixes" about unrelated things, while the
subject is the author's own summary. This is a known, noisy heuristic (SZZ papers
report a sizeable share of keyword matches aren't bug fixes); we document it and
keep the rules simple enough to check.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from review_map.errors import GitError, ReviewMapError

MAX_FILES = 50  # bigger commits are usually reformatting, vendoring or mass renames

_FIX = re.compile(r"\b(fix(es|ed|ing)?|bug(fix)?s?|regressions?|revert(s|ed)?|hotfix(es)?)\b", re.I)
# Matches that are almost never a bug in code: docs, typos, style, tests or CI only.
_NOT_FIX = re.compile(
    r"\b(typos?|spelling|docs?|documentation|readme|changelog|whitespace|pep ?8|lint(ing)?"
    r"|flake8|style|formatting|ci|tests? only)\b",
    re.I,
)

TRAIN_EXTRA_HINT = "Install the training extra: pip install 'review-map[train]'."


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    parent: str | None  # None for a root commit
    date: datetime  # committer date, UTC: when the change landed
    author: str
    subject: str
    files: int  # number of files changed
    is_fix: bool

    @property
    def is_huge(self) -> bool:
        return self.files > MAX_FILES


def is_fix_message(message: str) -> bool:
    subject = message.strip().splitlines()[0] if message.strip() else ""
    return bool(_FIX.search(subject)) and not _NOT_FIX.search(subject)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("git") is None:
        raise GitError("git is not installed or not on PATH.", hint="Install git and try again.")
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def resolve_repo(target: str, repos_dir: Path) -> Path:
    """A local repo path, or `owner/name` cloned (full history) into `repos_dir/name`."""
    path = Path(target).expanduser()
    if path.is_dir():
        return path
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", target) or ".." in target:
        raise ReviewMapError(
            f"Not a folder or a GitHub owner/name: {target!r}",
            hint="Pass a local repo path, or e.g. psf/requests to clone it into data/repos/.",
        )
    dest = repos_dir / target.split("/")[1]
    if dest.is_dir():
        return dest
    repos_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--quiet", f"https://github.com/{target}.git", str(dest)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise GitError(f"Could not clone {target}: {result.stderr.strip()}")
    return dest


def head_sha(repo: Path) -> str:
    result = _git(repo, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise GitError(f"Not a git repository with commits: {repo}")
    return result.stdout.strip()


def list_commits(repo: Path) -> list[CommitInfo]:
    """Every non-merge commit reachable from HEAD, oldest first."""
    try:
        from pydriller import Repository
    except ImportError:
        raise ReviewMapError("PyDriller is not installed.", hint=TRAIN_EXTRA_HINT) from None

    commits = []
    for c in Repository(str(repo), only_no_merge=True).traverse_commits():
        commits.append(
            CommitInfo(
                sha=c.hash,
                parent=c.parents[0] if c.parents else None,
                date=c.committer_date.astimezone(UTC),
                author=c.author.name,
                subject=c.msg.strip().splitlines()[0] if c.msg.strip() else "",
                files=c.files,
                is_fix=is_fix_message(c.msg),
            )
        )
    return commits


def changed_paths(repo: Path) -> dict[str, list[str]]:
    """Every file each non-merge commit touched (new paths for renames), in one git call."""
    result = _git(repo, "log", "--no-merges", "--no-renames", "--format=%x00%H", "--name-only")
    if result.returncode != 0:
        raise GitError(f"git log failed in {repo}: {result.stderr.strip()}")
    out: dict[str, list[str]] = {}
    for block in result.stdout.split("\x00")[1:]:
        sha, _, rest = block.partition("\n")
        out[sha.strip()] = [p for p in rest.split("\n") if p.strip()]
    return out

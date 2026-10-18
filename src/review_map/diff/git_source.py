"""Produce a unified diff between two refs of a local repo.

We shell out to the `git` command instead of using a Python git library: it is
always available where the user runs git, and its diff output is the reference.
"""

import shutil
import subprocess
from pathlib import Path

from review_map.errors import GitError

# Flags that make output independent of the user's personal git config
# (colors, external diff tools, `diff.noprefix`, quoted non-ASCII paths...).
_DIFF_FLAGS = [
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-relative",
    "--find-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("git") is None:
        raise GitError("git is not installed or not on PATH.", hint="Install git and try again.")
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def find_repo_root(path: Path) -> Path:
    """Return the top-level directory of the git repo containing `path`."""
    if not path.exists():
        raise GitError(f"Path does not exist: {path}")
    if not path.is_dir():
        raise GitError(f"Not a directory: {path}")
    result = _git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise GitError(
            f"Not a git repository: {path}",
            hint="Run this inside a git repo, or pass --repo PATH.",
        )
    return Path(result.stdout.strip())


def resolve_ref(repo: Path, ref: str) -> str:
    """Return the commit SHA for `ref`, or raise a friendly error."""
    if not ref or ref.startswith("-"):
        raise GitError(f"Invalid ref: {ref!r}")
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if result.returncode != 0:
        hint = "Check the name with `git branch -a` or `git log --oneline`."
        if not _has_commits(repo):
            hint = "This repository has no commits yet."
        raise GitError(f"Unknown git ref: {ref!r}", hint=hint)
    return result.stdout.strip()


def _has_commits(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0


def default_base(repo: Path) -> str:
    """Guess the base branch: origin's default branch, then main, then master."""
    result = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    candidates = [result.stdout.strip()] if result.returncode == 0 else []
    candidates += ["main", "master", "origin/main", "origin/master"]
    for name in candidates:
        if name and _git(repo, "rev-parse", "--verify", "--quiet", name).returncode == 0:
            return name
    raise GitError(
        "Could not guess the base branch (no main or master).",
        hint="Pass it explicitly with --base BRANCH.",
    )


def get_diff(repo: Path, base: str, head: str) -> str:
    """Unified diff of what `head` changed since it branched from `base` (`base...head`)."""
    base_sha = resolve_ref(repo, base)
    head_sha = resolve_ref(repo, head)
    result = _git(repo, "diff", *_DIFF_FLAGS, f"{base_sha}...{head_sha}", "--")
    if result.returncode != 0:
        message = result.stderr.strip() or "git diff failed"
        if "no merge base" in message:
            raise GitError(
                f"{base!r} and {head!r} have no common history.",
                hint="Make sure both refs come from the same repository.",
            )
        raise GitError(f"git diff failed: {message}")
    return result.stdout

"""Fetch a public GitHub PR's description and commits, so it can be analysed locally.

The API gives the title, description and the base/head SHAs. The code itself
comes from a cached partial clone (`--filter=blob:none`: commits and trees now,
file contents only when git needs them), because mapping lines to functions
needs whole files, not just the diff. Later runs on the same repo only fetch the
new commits, and re-running the same PR needs no network for git at all.
"""

import base64
import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from review_map.config import CONFIG_FILENAMES, Config, LoadedConfig, load_config, parse_config
from review_map.diff import git_source
from review_map.errors import GitError, ReviewMapError
from review_map.github.client import GitHubClient, GitHubError, token_hint

ENV_REPO_CACHE_DIR = "REVIEW_MAP_REPO_CACHE_DIR"

# GitHub owners: letters, digits and single hyphens. Repos also allow `.` and `_`.
_NAME = r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100})"
_SHORT = re.compile(rf"^{_NAME}#(?P<number>\d+)$")
_URL = re.compile(
    rf"^(?:https?://)?(?:www\.)?github\.com/{_NAME}/pull/(?P<number>\d+)(?:[/?#].*)?$"
)


@dataclass(frozen=True)
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def __str__(self) -> str:
        return f"{self.slug}#{self.number}"


def parse_pr_ref(text: str) -> PRRef:
    """`owner/repo#123` or `https://github.com/owner/repo/pull/123` → PRRef."""
    text = text.strip()
    match = _SHORT.match(text) or _URL.match(text)
    if match is None or match["repo"] in (".", "..") or match["repo"].endswith(".git"):
        raise ReviewMapError(
            f"Not a pull request reference: {text!r}",
            hint="Use owner/repo#123 or https://github.com/owner/repo/pull/123.",
        )
    number = int(match["number"])
    if number < 1:
        raise ReviewMapError(f"Not a valid pull request number: {number}")
    return PRRef(match["owner"], match["repo"], number)


class PullRequest(BaseModel):
    ref: str  # owner/repo#number as asked
    repo: str  # owner/repo as GitHub reports it (differs if the repo was renamed)
    number: int
    title: str
    body: str  # "" when the PR has no description
    state: str  # "open" or "closed"
    merged: bool
    draft: bool
    html_url: str
    base_ref: str  # branch name, e.g. "main"
    base_sha: str
    head_label: str  # e.g. "someone:fix-timeout"
    head_sha: str
    from_fork: bool

    @property
    def status(self) -> str:
        if self.merged:
            return "merged"
        if self.draft and self.state == "open":
            return "draft"
        return self.state

    @property
    def description(self) -> str | None:
        """Title plus body as the claimed intent, or None when there is no body.

        A title alone is too thin to verify against: every change would look unclaimed.
        """
        if not self.body.strip():
            return None
        return f"{self.title}\n\n{self.body}"


def pr_from_api(ref: PRRef, data: Any) -> PullRequest:
    try:
        base, head = data["base"], data["head"]
        base_repo = base["repo"]["full_name"]
        head_repo = (head.get("repo") or {}).get("full_name")  # None if the fork was deleted
        return PullRequest(
            ref=str(ref),
            repo=base_repo,
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            state=str(data["state"]),
            merged=bool(data.get("merged") or data.get("merged_at")),
            draft=bool(data.get("draft")),
            html_url=str(data["html_url"]),
            base_ref=str(base["ref"]),
            base_sha=str(base["sha"]),
            head_label=str(head.get("label") or head["ref"]),
            head_sha=str(head["sha"]),
            from_fork=head_repo != base_repo,
        )
    except (KeyError, TypeError, ValueError):
        raise GitHubError(f"GitHub's answer for {ref} is missing expected fields.") from None


def fetch_pr(client: GitHubClient, ref: PRRef) -> PullRequest:
    data = client.get_json(f"/repos/{ref.slug}/pulls/{ref.number}", what=f"pull request {ref}")
    return pr_from_api(ref, data)


# --- Local clone cache ------------------------------------------------------------


def default_repo_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """`$REVIEW_MAP_REPO_CACHE_DIR`, else `$XDG_CACHE_HOME/review-map/repos`, else ~/.cache/..."""
    env = os.environ if env is None else env
    if env.get(ENV_REPO_CACHE_DIR):
        return Path(env[ENV_REPO_CACHE_DIR]).expanduser()
    base = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else Path.home() / ".cache"
    return base / "review-map" / "repos"


def clone_path(cache_dir: Path, repo: str) -> Path:
    # GitHub names are case-insensitive; one folder per repo.
    return cache_dir / (repo.replace("/", "__").lower() + ".git")


def _auth_config(token: str | None) -> dict[str, str]:
    """Git env vars that send the token to github.com only, without putting it in argv or on disk.

    Same approach as actions/checkout: an `http.<url>.extraheader` passed through
    `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`.
    """
    env = {"GIT_TERMINAL_PROMPT": "0"}  # never hang on a username prompt
    if not token:
        return env
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env |= {
        "GIT_CONFIG_COUNT": str(count + 1),
        f"GIT_CONFIG_KEY_{count}": "http.https://github.com/.extraheader",
        f"GIT_CONFIG_VALUE_{count}": f"AUTHORIZATION: basic {basic}",
    }
    return env


@contextmanager
def git_auth(token: str | None) -> Iterator[None]:
    """Set the git auth env vars for this process while the PR is being analysed.

    Process-wide, because a partial clone fetches missing file contents lazily
    from inside ordinary `git diff`/`git cat-file` calls in `git_source`.
    """
    changes = _auth_config(token)
    saved = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _git(
    repo: Path, *args: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    if shutil.which("git") is None:
        raise GitError("git is not installed or not on PATH.", hint="Install git and try again.")
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ, **env} if env else None,
    )


def _has_commit(repo: Path, sha: str) -> bool:
    # In a partial clone, asking for a missing object quietly fetches just that one
    # object ("lazy fetch"). Here that would hide a missing commit, and its history
    # would then arrive one commit at a time. GIT_NO_LAZY_FETCH needs git 2.44+.
    result = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", env={"GIT_NO_LAZY_FETCH": "1"})
    return result.returncode == 0


def _fetch_error(stderr: str, pr: PullRequest) -> GitError:
    text = stderr.lower()
    if "could not resolve host" in text or "failed to connect" in text:
        return GitError(
            "Could not reach github.com to fetch the PR's commits.",
            hint="Check your connection and try again.",
        )
    if "repository not found" in text or "could not read username" in text:
        return GitError(
            f"git could not read {pr.repo}.",
            hint=token_hint(private=True),
        )
    if "not our ref" in text or "couldn't find remote ref" in text:
        return GitError(
            f"GitHub no longer has the commits of {pr.ref}.",
            hint="The branch may have been force-pushed or deleted. Try again in a minute.",
        )
    last = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown error"
    return GitError(f"git fetch failed: {last}")


def ensure_pr_commits(
    pr: PullRequest, cache_dir: Path, remote_url: str | None = None
) -> tuple[Path, bool]:
    """Make sure the cached clone has the PR's base and head commits.

    Returns the clone's path and whether anything had to be fetched.
    Call inside `git_auth(...)` for private repos.
    """
    path = clone_path(cache_dir, pr.repo)
    remote_url = remote_url or f"https://github.com/{pr.repo}.git"
    if path.exists():
        check = _git(path, "rev-parse", "--is-bare-repository")
        if check.returncode != 0 or check.stdout.strip() != "true":
            raise GitError(
                f"The cached clone at {path} is broken.",
                hint="Delete that folder and run the command again.",
            )
    else:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GitError(
                f"Could not create the repo cache at {path.parent}: {exc.strerror or exc}",
                hint=f"Set {ENV_REPO_CACHE_DIR} or pass --cache-dir to a writable folder.",
            ) from None
        init = _git(path.parent, "init", "--quiet", "--bare", path.name)
        if init.returncode != 0:
            raise GitError(f"Could not create a git repo at {path}: {init.stderr.strip()}")
        _git(path, "remote", "add", "origin", remote_url)

    missing = [sha for sha in (pr.base_sha, pr.head_sha) if not _has_commit(path, sha)]
    if not missing:
        return path, False
    # Fetch by SHA: GitHub serves any commit reachable from a ref, and PR heads from
    # forks are reachable through refs/pull/N/head in the base repo.
    result = _git(path, "fetch", "--quiet", "--no-tags", "--filter=blob:none", "origin", *missing)
    if result.returncode != 0 or any(not _has_commit(path, sha) for sha in missing):
        raise _fetch_error(result.stderr, pr)
    return path, True


def load_pr_config(clone: Path, base_sha: str, explicit: Path | None) -> LoadedConfig:
    """`--config` if given, else `.reviewmap.yml` from the PR's *base* commit, else defaults.

    Never the head: the PR's own config is untrusted, and could otherwise ignore
    the very files it changes.
    """
    if explicit is not None:
        return load_config(clone, explicit)
    for name in CONFIG_FILENAMES:
        text = git_source.read_file(clone, base_sha, name)
        if text is not None:
            shown = f"{name} (from base)"
            return LoadedConfig(config=parse_config(text, shown), source=Path(name))
    return LoadedConfig(config=Config(), source=None)

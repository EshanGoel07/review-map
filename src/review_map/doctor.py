"""`review-map doctor`: check the setup and say how to fix each problem (Phase 12).

Each check gives ✓ (fine), ✗ (broken) or – (optional, not set up), a one-line detail
and, when something can be better, how to fix it. Key and token values are never
printed, only whether they are set and whether they work.

Required checks (git, Python, config) decide the exit code: Review Map can run
without an LLM key or a GitHub token, so problems there are reported but don't
make the setup "unusable".
"""

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from review_map.claims.llm import (
    PROVIDERS,
    ResolvedLLM,
    default_cache_dir,
    make_backend,
    resolve_llm,
    setup_hint,
)
from review_map.config import LLMSettings, load_config
from review_map.diff import git_source
from review_map.diff.github_pr import default_repo_cache_dir
from review_map.errors import ReviewMapError
from review_map.github.client import GitHubClient, token_from_env
from review_map.risk.scorer import REPO_MODEL_DIR, choose_model, folder_reader

MIN_PYTHON = (3, 11)
TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"


@dataclass
class Check:
    name: str
    ok: bool | None  # True ✓, False ✗, None – (optional, not set up / skipped)
    detail: str
    fix: str | None = None
    required: bool = False

    @property
    def symbol(self) -> str:
        return {True: "✓", False: "✗", None: "–"}[self.ok]


def check_git() -> Check:
    if shutil.which("git") is None:
        return Check(
            "git", False, "not found on PATH", "Install git: https://git-scm.com/downloads", True
        )
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
    return Check("git", True, out.stdout.strip() or "installed", required=True)


def check_python(version: tuple[int, ...] = tuple(sys.version_info[:3])) -> Check:
    shown = ".".join(map(str, version))
    if tuple(version[:2]) < MIN_PYTHON:
        return Check(
            "Python",
            False,
            f"{shown} (needs 3.11 or newer)",
            "Install Python 3.11+ and reinstall Review Map with it.",
            True,
        )
    return Check("Python", True, shown, required=True)


def check_config(repo: Path) -> tuple[Check, Path | None]:
    """The config, and the repo root (None when `repo` isn't in a git repo)."""
    try:
        root = git_source.find_repo_root(repo)
    except ReviewMapError:
        return Check(
            "Config",
            None,
            f"{repo} is not a git repo: defaults apply",
            "Run doctor inside the repo you want to review, or pass --repo.",
        ), None
    try:
        loaded = load_config(root)
    except ReviewMapError as exc:
        return Check(".reviewmap.yml", False, exc.message, exc.hint, True), root
    if loaded.source is None:
        return Check("Config", True, "no .reviewmap.yml: defaults (optional)", required=True), root
    return Check(".reviewmap.yml", True, f"{loaded.source.name} is valid", required=True), root


Probe = Callable[[ResolvedLLM], None]


def probe_llm(resolved: ResolvedLLM) -> None:
    """One tiny request; raises ReviewMapError (or another error) if it fails."""
    reply = make_backend(resolved).complete(
        'Reply with the JSON object {"ok": true} and nothing else.', "ping"
    )
    if not reply.strip():
        raise ReviewMapError("empty reply")


def check_llm(root: Path | None, env: Mapping[str, str], offline: bool, probe: Probe) -> Check:
    try:
        settings = load_config(root).config.llm if root else None
    except ReviewMapError:
        settings = None
    try:
        resolved = resolve_llm(settings or LLMSettings(), env)
    except ReviewMapError as exc:
        return Check("LLM", False, exc.message, exc.hint)
    if resolved is None:
        return Check(
            "LLM",
            None,
            "none: descriptions are read by a heuristic parser (low confidence)",
            f"Run `review-map setup`. {setup_hint()}",
        )
    info = PROVIDERS[resolved.provider]
    key = f", {info.key_env} set" if info.key_env else ""
    what = f"{resolved.label}{key}"
    if offline:
        return Check("LLM", True, f"{what} (not tested: --offline)")
    try:
        probe(resolved)
    except ReviewMapError as exc:
        return Check("LLM", False, f"{what}: {exc.message}", exc.hint or "Run `review-map setup`.")
    except Exception as exc:  # a retryable error (timeout, rate limit) still means "not working"
        return Check(
            "LLM",
            False,
            f"{what}: {exc}",
            "Check your network and the provider's status, then try again.",
        )
    return Check("LLM", True, f"{what}, test call worked")


RateProbe = Callable[[str | None], dict]


def probe_rate_limit(token: str | None) -> dict:
    with GitHubClient(token) as client:
        return client.get_json("/rate_limit", what="the rate limit")["resources"]["core"]


def check_github(env: Mapping[str, str], offline: bool, probe: RateProbe) -> Check:
    token = token_from_env(env)
    name = "GitHub token"
    if offline:
        return Check(
            name,
            True if token else None,
            "GITHUB_TOKEN set (not tested: --offline)" if token else "GITHUB_TOKEN not set",
            None if token else f"Optional. Create one at {TOKEN_URL}",
        )
    try:
        core = probe(token)
    except ReviewMapError as exc:
        return Check(name, False, exc.message, exc.hint)
    except Exception as exc:
        return Check(name, False, f"could not reach GitHub: {exc}", "Check your network.")
    left = f"{core.get('remaining')}/{core.get('limit')} API requests left this hour"
    if token:
        return Check(name, True, f"GITHUB_TOKEN works: {left}")
    return Check(
        name,
        None,
        f"GITHUB_TOKEN not set: {left} (public repos only)",
        f"Optional: a token gives 5,000 requests/hour and private repos. {TOKEN_URL}",
    )


def _writable(folder: Path) -> str | None:
    """None if `folder` can be created and written to, else the reason."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder):
            pass
    except OSError as exc:
        return exc.strerror or str(exc)
    return None


def check_caches(env: Mapping[str, str]) -> Check:
    problems = []
    for label, folder, var in (
        ("LLM cache", default_cache_dir(env), "REVIEW_MAP_CACHE_DIR"),
        ("repo cache", default_repo_cache_dir(env), "REVIEW_MAP_REPO_CACHE_DIR"),
    ):
        reason = _writable(folder)
        if reason:
            problems.append((f"{label} {folder}: {reason}", var))
    if problems:
        return Check(
            "Cache folders",
            False,
            "; ".join(p for p, _ in problems),
            "Set " + " / ".join(v for _, v in problems) + " to a writable folder.",
        )
    return Check("Cache folders", True, "writable")


def check_model(root: Path | None) -> Check:
    folder = root / REPO_MODEL_DIR if root else None
    choice = choose_model(folder_reader(folder) if folder else None, str(folder))
    if choice.model is None:
        return Check(
            "Risk model",
            False,
            "; ".join(choice.notes) or "no model",
            "Reinstall Review Map; the rules still work meanwhile.",
        )
    detail = choice.model.label + (
        " (not calibrated: shown as levels)" if not choice.model.calibrated else ""
    )
    fix = " ".join(choice.notes) or None
    return Check("Risk model", not choice.notes, detail, fix)


def check_env_ignored(root: Path | None) -> Check | None:
    if root is None or not (root / ".env").is_file():
        return None
    out = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", ".env"], capture_output=True, check=False
    )
    if out.returncode == 0:
        return Check(".env", True, "gitignored")
    return Check(
        ".env",
        False,
        "exists but is NOT gitignored: your keys could be committed",
        "Add `.env` to .gitignore.",
    )


def run_checks(
    repo: Path,
    env: Mapping[str, str],
    *,
    offline: bool = False,
    llm_probe: Probe = probe_llm,
    rate_probe: RateProbe = probe_rate_limit,
) -> list[Check]:
    config, root = check_config(repo)
    checks = [check_git(), check_python(), config]
    checks.append(check_llm(root, env, offline, llm_probe))
    checks.append(check_github(env, offline, rate_probe))
    checks.append(check_caches(env))
    checks.append(check_model(root))
    env_check = check_env_ignored(root)
    if env_check:
        checks.append(env_check)
    return checks


def usable(checks: list[Check]) -> bool:
    return not any(c.required and c.ok is False for c in checks)

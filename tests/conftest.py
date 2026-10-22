"""Shared test helpers: isolated git config and a tiny repo builder."""

import socket
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Ignore the developer's ~/.gitconfig so tests behave the same everywhere."""
    empty = tmp_path_factory.mktemp("gitconfig") / "config"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var, value in {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(var, value)


_LLM_ENV_VARS = (
    "REVIEW_MAP_LLM_PROVIDER",
    "REVIEW_MAP_LLM_MODEL",
    "REVIEW_MAP_LLM_BASE_URL",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Tests never see the developer's keys or .env, and never touch the real cache."""
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REVIEW_MAP_CACHE_DIR", str(tmp_path_factory.mktemp("llm-cache")))
    monkeypatch.setattr("review_map.cli.load_env_file", lambda: None)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch):
    """Fail loudly if any test opens a network connection (git subprocesses are unaffected)."""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        raise RuntimeError(f"Tests must not use the network (tried to connect to {address!r})")

    real_getaddrinfo = socket.getaddrinfo

    def guarded_lookup(host, *args, **kwargs):
        if host in ("localhost", "127.0.0.1", "::1", None):
            return real_getaddrinfo(host, *args, **kwargs)
        raise RuntimeError(f"Tests must not use the network (tried to resolve {host!r})")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_lookup)


class GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def write(self, rel: str, content: str | bytes) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)

    def commit(self, message: str = "commit") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    return GitRepo(tmp_path / "repo")

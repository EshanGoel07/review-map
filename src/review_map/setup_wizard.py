"""`review-map setup`: choose an LLM, paste keys, and write them to `.env` (Phase 12).

The parts that touch the file are plain functions (`plan_updates`, `update_env_text`)
so they can be tested without a terminal. The CLI asks the questions.

Rules: keys are read with hidden input and never printed; an existing `.env` is
updated in place (other lines are kept) and only after the user confirms; a value
that is already set is only replaced after its own confirmation; the file is made
readable by its owner only; and a warning (with an offer to fix it) appears when
`.env` isn't gitignored.
"""

import contextlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from review_map.claims.llm import ENV_MODEL, ENV_PROVIDER, PROVIDERS

CHOICES = ["groq", "gemini", "anthropic", "openai", "ollama", "none"]
DEFAULT_CHOICE = "groq"
KEY_URLS = {
    "groq": "https://console.groq.com/keys (free tier)",
    "gemini": "https://aistudio.google.com/apikey (free tier)",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
}
CHOICE_NOTES = {
    "groq": "free tier, fast (default)",
    "gemini": "free tier",
    "anthropic": "paid",
    "openai": "paid",
    "ollama": "local and offline, no key; run `ollama serve`",
    "none": "no LLM: a heuristic parser reads descriptions (low confidence)",
}
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


@dataclass
class Answers:
    provider: str  # one of CHOICES
    api_key: str | None = None
    model: str | None = None  # None: the provider's default
    github_token: str | None = None


def plan_updates(a: Answers) -> dict[str, str]:
    """The `.env` variables to set for these answers."""
    updates: dict[str, str] = {}
    if a.provider != "none":
        info = PROVIDERS[a.provider]
        updates[ENV_PROVIDER] = a.provider
        updates[ENV_MODEL] = a.model or info.default_model
        if info.key_env and a.api_key:
            updates[info.key_env] = a.api_key
    if a.github_token:
        updates["GITHUB_TOKEN"] = a.github_token
    return updates


def read_values(text: str) -> dict[str, str]:
    """Uncommented `NAME=value` lines (the last one wins, like python-dotenv)."""
    values = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            values[m.group(1)] = m.group(2).strip().strip("'\"")
    return values


def update_env_text(text: str, updates: dict[str, str]) -> str:
    """Set each variable in place where it already appears uncommented, else append it."""
    lines = text.splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(1) in updates:
            name = m.group(1)
            if name in done:
                lines[i] = f"# {line}  (replaced by review-map setup)"
                continue
            lines[i] = f"{name}={updates[name]}"
            done.add(name)
    new = [f"{k}={v}" for k, v in updates.items() if k not in done]
    if new:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["# Added by review-map setup", *new]
    return "\n".join(lines) + "\n"


def conflicts(text: str, updates: dict[str, str]) -> list[str]:
    """Variables that already have a different, non-empty value."""
    current = read_values(text)
    return [k for k, v in updates.items() if current.get(k) and current[k] != v]


def write_env(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)  # keys: readable by the owner only


def is_gitignored(path: Path) -> bool | None:
    """True/False inside a git repo, None outside one."""
    inside = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        check=False,
    )
    if inside.returncode != 0:
        return None
    out = subprocess.run(
        ["git", "-C", str(path.parent), "check-ignore", "-q", path.name],
        capture_output=True,
        check=False,
    )
    return out.returncode == 0


def add_to_gitignore(folder: Path, name: str = ".env") -> None:
    gitignore = folder / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    gitignore.write_text(text + f"{name}\n", encoding="utf-8")


NON_INTERACTIVE_STEPS = """\
Set up Review Map without the wizard:

1. Pick an LLM provider to read PR descriptions (optional; without one a heuristic
   parser is used). Groq is the default and has a free tier:
     https://console.groq.com/keys
   Others: Gemini (free tier), Anthropic, OpenAI, or local Ollama (no key).
2. Put the settings in a .env file in your repo (see .env.example), e.g.
     REVIEW_MAP_LLM_PROVIDER=groq
     REVIEW_MAP_LLM_MODEL=openai/gpt-oss-120b
     GROQ_API_KEY=...
   or export them as environment variables (these win over .env).
3. Optional: GITHUB_TOKEN=... for 5,000 GitHub API requests an hour and private repos:
     https://github.com/settings/personal-access-tokens/new
4. Make sure .env is in .gitignore.
5. Check everything with: review-map doctor

In GitHub Actions, use repository secrets instead of .env (see examples/workflow.yml).
"""

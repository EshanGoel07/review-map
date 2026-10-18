"""Load and validate the optional `.reviewmap.yml` config.

Every setting has a default, so the file is optional. Unknown keys are rejected
so typos (e.g. `ignore_paths` instead of `ignore`) are reported instead of
silently doing nothing.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_map.errors import ConfigError

CONFIG_FILENAMES = (".reviewmap.yml", ".reviewmap.yaml")

LLMProvider = Literal["groq", "gemini", "anthropic", "openai", "ollama"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadingBudget(_Strict):
    """How much of the change the reading plan asks you to read carefully."""

    lines_fraction: float = Field(default=0.15, gt=0, le=1)
    max_units: int = Field(default=5, ge=1)


class LLMSettings(_Strict):
    """LLM choice; env vars override these (Phase 4)."""

    provider: LLMProvider | None = None
    model: str | None = None


class Config(_Strict):
    ignore: list[str] = Field(default_factory=list)
    sensitive_paths: list[str] = Field(default_factory=list)
    reading_budget: ReadingBudget = Field(default_factory=ReadingBudget)
    llm: LLMSettings = Field(default_factory=LLMSettings)


class LoadedConfig(BaseModel):
    config: Config
    source: Path | None  # None means defaults (no config file found)


def find_config(repo_root: Path) -> Path | None:
    for name in CONFIG_FILENAMES:
        candidate = repo_root / name
        if candidate.is_file():
            return candidate
    return None


def load_config(repo_root: Path, explicit: Path | None = None) -> LoadedConfig:
    """Load config from `explicit`, else from `.reviewmap.yml` in `repo_root`, else defaults."""
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"Config file not found: {explicit}")
        path: Path | None = explicit
    else:
        path = find_config(repo_root)

    if path is None:
        return LoadedConfig(config=Config(), source=None)
    shown = explicit if explicit is not None else path.name
    return LoadedConfig(config=parse_config(path.read_text(encoding="utf-8"), shown), source=path)


def parse_config(text: str, path: Path | str = ".reviewmap.yml") -> Config:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" (line {mark.line + 1}, column {mark.column + 1})"
        raise ConfigError(
            f"{path} is not valid YAML{where}.",
            hint="Check indentation and that lists use '- item'.",
        ) from None

    if data is None:  # empty file
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping of settings, got {type(data).__name__}.")

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        problems = "\n".join(
            f"  - {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(
            f"{path} has invalid settings:\n{problems}",
            hint="See examples/reviewmap.yml for all supported settings.",
        ) from None

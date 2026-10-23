"""Provider-agnostic LLM client: JSON output, retries with backoff, and a disk cache.

Two backends cover every supported provider:
- `OpenAICompatBackend`: the `openai` SDK with a `base_url`, for Groq, Gemini,
  OpenAI and Ollama (they all speak the OpenAI chat-completions format).
- `AnthropicBackend`: the official `anthropic` SDK.

`LLMClient.complete_json()` asks for a JSON object, validates it with a pydantic
model, asks once more (quoting the validation error) if it doesn't fit, and caches
only validated answers.

Settings come from env vars first, then `.reviewmap.yml`, then auto-detection
from whichever API key is set (Groq first, then Gemini, Anthropic, OpenAI).
"""

import hashlib
import json
import os
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

import anthropic
import openai
from pydantic import BaseModel, ValidationError

from review_map.config import LLMSettings
from review_map.errors import LLMError

T = TypeVar("T", bound=BaseModel)

ENV_PROVIDER = "REVIEW_MAP_LLM_PROVIDER"
ENV_MODEL = "REVIEW_MAP_LLM_MODEL"
ENV_BASE_URL = "REVIEW_MAP_LLM_BASE_URL"
ENV_CACHE_DIR = "REVIEW_MAP_CACHE_DIR"

TIMEOUT_SECONDS = 60.0
MAX_TRANSIENT_RETRIES = 3  # rate limits, timeouts, 5xx: wait 1s, 2s, 4s (+ jitter)
MAX_RETRY_AFTER_SECONDS = 20.0
INVALID_OUTPUT_ATTEMPTS = 2  # first try + one repair request quoting the error


@dataclass(frozen=True)
class ProviderInfo:
    key_env: str | None  # None: no key needed (local Ollama)
    default_model: str
    base_url: str | None = None  # None: the SDK's default endpoint
    anthropic_sdk: bool = False


PROVIDERS: dict[str, ProviderInfo] = {
    "groq": ProviderInfo("GROQ_API_KEY", "openai/gpt-oss-120b", "https://api.groq.com/openai/v1"),
    "gemini": ProviderInfo(
        "GEMINI_API_KEY",
        "gemini-2.5-flash",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    ),
    "anthropic": ProviderInfo("ANTHROPIC_API_KEY", "claude-haiku-5-5", anthropic_sdk=True),
    "openai": ProviderInfo("OPENAI_API_KEY", "gpt-4o-mini"),
    "ollama": ProviderInfo(None, "llama3.1:8b", "http://localhost:11434/v1"),
}

# Used when no provider is chosen explicitly: the first one with a key wins.
AUTO_DETECT_ORDER = ("groq", "gemini", "anthropic", "openai")


@dataclass(frozen=True)
class ResolvedLLM:
    provider: str
    model: str
    api_key: str | None
    base_url: str | None

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


def resolve_llm(settings: LLMSettings, env: Mapping[str, str] | None = None) -> ResolvedLLM | None:
    """Pick provider, model, key and URL. None means no LLM is configured.

    Raises `LLMError` when a provider is chosen explicitly but can't be used
    (unknown name, or its API key is missing).
    """
    env = os.environ if env is None else env
    env_provider = env.get(ENV_PROVIDER, "").strip().lower()

    if env_provider:
        if env_provider not in PROVIDERS:
            raise LLMError(
                f"{ENV_PROVIDER}={env_provider!r} is not a supported provider.",
                hint=f"Use one of: {', '.join(PROVIDERS)}.",
            )
        provider = env_provider
    elif settings.provider:
        provider = settings.provider
    else:
        provider = next((p for p in AUTO_DETECT_ORDER if env.get(PROVIDERS[p].key_env or "")), "")
        if not provider:
            return None

    info = PROVIDERS[provider]
    api_key = env.get(info.key_env, "").strip() if info.key_env else None
    if info.key_env and not api_key:
        raise LLMError(
            f"The LLM provider is {provider}, but {info.key_env} is not set.",
            hint=key_hint(info.key_env),
        )

    # A model from the config file only applies to the provider it was written for.
    config_model = settings.model if settings.provider in (None, provider) else None
    model = env.get(ENV_MODEL, "").strip() or config_model or info.default_model
    base_url = env.get(ENV_BASE_URL, "").strip() or info.base_url
    return ResolvedLLM(provider, model, api_key, base_url)


def in_github_actions(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("GITHUB_ACTIONS") == "true"


def key_hint(key_env: str) -> str:
    """Where to put an API key: a repo secret inside GitHub Actions, else `.env`."""
    if in_github_actions():
        return (
            f"Add a {key_env} repository secret and pass it to the action as an env var "
            "(see examples/workflow.yml)."
        )
    return f"Add {key_env}=... to .env (see .env.example)."


def setup_hint() -> str:
    if in_github_actions():
        return f"To enable the LLM parser: {key_hint('GROQ_API_KEY')}"
    return "Set GROQ_API_KEY in .env to enable the LLM parser (see .env.example)."


# --- Backends ----------------------------------------------------------------


class TransientLLMError(Exception):
    """Worth retrying after a pause: rate limit, timeout, server error."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class InvalidOutputError(Exception):
    """The model answered, but not with a usable JSON object."""


class Backend(Protocol):
    def complete(self, system: str, user: str) -> str: ...


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _classify(exc: Exception, settings: ResolvedLLM) -> Exception:
    """Turn an SDK exception into a transient, invalid-output, or user-facing error."""
    who = settings.provider
    if isinstance(exc, openai.APITimeoutError | anthropic.APITimeoutError):
        return TransientLLMError(f"{who} timed out")
    if isinstance(exc, openai.APIConnectionError | anthropic.APIConnectionError):
        return TransientLLMError(f"could not reach {who} ({settings.base_url or 'default URL'})")

    status = getattr(exc, "status_code", None)
    message = str(getattr(exc, "message", "") or exc)
    if status == 429 or (status is not None and status >= 500):
        return TransientLLMError(f"{who} returned HTTP {status}", _retry_after(exc))
    if status in (401, 403):
        info = PROVIDERS[who]
        return LLMError(
            f"{who} rejected the API key (HTTP {status}).",
            hint=f"Check {info.key_env} in .env." if info.key_env else None,
        )
    if status == 404:
        return LLMError(
            f"{who} has no model named {settings.model!r} (HTTP 404).",
            hint=f"Set {ENV_MODEL} to a model your provider offers.",
        )
    if status == 400 and "json_validate_failed" in message:  # Groq: model broke JSON mode
        return InvalidOutputError("the reply was not valid JSON")
    return LLMError(f"{who} request failed (HTTP {status}): {message[:200]}")


class OpenAICompatBackend:
    """Groq, Gemini, OpenAI and Ollama through the OpenAI chat-completions API."""

    def __init__(self, settings: ResolvedLLM, client: Any = None) -> None:
        self.settings = settings
        self.client = client or openai.OpenAI(
            api_key=settings.api_key or "ollama",  # Ollama ignores the key but the SDK needs one
            base_url=settings.base_url,
            timeout=TIMEOUT_SECONDS,
            max_retries=0,  # LLMClient does the retrying
        )

    def complete(self, system: str, user: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.settings.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
        except (openai.APIError, anthropic.APIError) as exc:
            raise _classify(exc, self.settings) from None
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise InvalidOutputError("the reply was empty")
        return content


class AnthropicBackend:
    """Claude through the official SDK. No JSON mode, so the prompt asks for JSON."""

    def __init__(self, settings: ResolvedLLM, client: Any = None) -> None:
        self.settings = settings
        self.client = client or anthropic.Anthropic(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=TIMEOUT_SECONDS,
            max_retries=0,
        )

    def complete(self, system: str, user: str) -> str:
        try:
            response = self.client.messages.create(
                model=self.settings.model,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except (openai.APIError, anthropic.APIError) as exc:
            raise _classify(exc, self.settings) from None
        text = "".join(getattr(b, "text", "") for b in response.content if b.type == "text")
        if not text:
            raise InvalidOutputError("the reply was empty")
        return text


def make_backend(settings: ResolvedLLM) -> Backend:
    if PROVIDERS[settings.provider].anthropic_sdk:
        return AnthropicBackend(settings)
    return OpenAICompatBackend(settings)


# --- Cache -------------------------------------------------------------------


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """`$REVIEW_MAP_CACHE_DIR`, else `$XDG_CACHE_HOME/review-map/llm`, else `~/.cache/...`."""
    env = os.environ if env is None else env
    if env.get(ENV_CACHE_DIR):
        return Path(env[ENV_CACHE_DIR]).expanduser()
    base = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else Path.home() / ".cache"
    return base / "review-map" / "llm"


class DiskCache:
    """One JSON file per answer, named by a hash of everything that shaped it.

    The cache is a convenience: unreadable entries are ignored and write
    failures (e.g. a read-only home directory) are silently skipped.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> str | None:
        try:
            return self._path(key).read_text(encoding="utf-8")
        except OSError:
            return None

    def set(self, key: str, value: str) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = self._path(key).with_suffix(".tmp")
            tmp.write_text(value, encoding="utf-8")
            tmp.replace(self._path(key))  # atomic: never a half-written entry
        except OSError:
            pass


# --- Client ------------------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def extract_json(text: str) -> str:
    """The JSON object inside a reply, tolerating code fences and stray prose."""
    text = _FENCE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise InvalidOutputError("the reply contained no JSON object")
    return text[start : end + 1]


def _validation_summary(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()[:5]
    )


@dataclass
class Completion(Generic[T]):
    value: T
    cached: bool


class LLMClient:
    def __init__(
        self,
        settings: ResolvedLLM,
        backend: Backend | None = None,
        cache: DiskCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.backend = backend or make_backend(settings)
        self.cache = cache
        self.sleep = sleep

    def _call(self, system: str, user: str) -> str:
        """One completion, retrying transient failures with exponential backoff."""
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            try:
                return self.backend.complete(system, user)
            except TransientLLMError as exc:
                if attempt == MAX_TRANSIENT_RETRIES:
                    raise LLMError(
                        f"{exc} (gave up after {MAX_TRANSIENT_RETRIES} retries).",
                        hint="Wait a minute and try again, or switch provider in .env.",
                    ) from None
                delay = 2**attempt + random.uniform(0, 0.5)
                if exc.retry_after is not None:
                    delay = max(delay, min(exc.retry_after, MAX_RETRY_AFTER_SECONDS))
                self.sleep(delay)
        raise AssertionError("unreachable")

    def complete_json(self, system: str, prompt: str, schema: type[T]) -> Completion[T]:
        """Ask for a JSON object matching `schema`; return it validated."""
        s = self.settings
        key = DiskCache.key(
            s.provider,
            s.model,
            s.base_url or "",
            system,
            prompt,
            json.dumps(schema.model_json_schema()),
        )
        if self.cache is not None and (hit := self.cache.get(key)) is not None:
            try:
                return Completion(schema.model_validate_json(hit), cached=True)
            except ValidationError:
                pass  # stale or corrupt entry: ask again

        user = prompt
        problem = ""
        for _ in range(INVALID_OUTPUT_ATTEMPTS):
            try:
                value = schema.model_validate_json(extract_json(self._call(system, user)))
            except InvalidOutputError as exc:
                problem = str(exc)
            except ValidationError as exc:
                problem = f"the JSON did not match the schema ({_validation_summary(exc)})"
            else:
                if self.cache is not None:
                    self.cache.set(key, value.model_dump_json())
                return Completion(value, cached=False)
            user = (
                f"{prompt}\n\nYour previous reply was rejected: {problem}. "
                "Reply again with only a JSON object in the required format."
            )
        raise LLMError(f"{s.label} returned unusable output twice: {problem}.")


def build_client(
    settings: LLMSettings, *, use_cache: bool = True, env: Mapping[str, str] | None = None
) -> LLMClient | None:
    """An `LLMClient` from settings and env, or None if no LLM is configured."""
    resolved = resolve_llm(settings, env)
    if resolved is None:
        return None
    return LLMClient(resolved, cache=DiskCache(default_cache_dir(env)) if use_cache else None)

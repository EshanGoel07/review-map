"""Tests for the LLM layer: settings, backends, retries, validation and cache.

No test talks to a real API: the SDK clients are replaced by small fakes, and
conftest blocks network connections.
"""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest
from pydantic import BaseModel

from review_map.claims import llm
from review_map.claims.llm import (
    AnthropicBackend,
    DiskCache,
    LLMClient,
    OpenAICompatBackend,
    ResolvedLLM,
    build_client,
    default_cache_dir,
    extract_json,
    resolve_llm,
)
from review_map.config import LLMSettings
from review_map.errors import LLMError

GROQ = ResolvedLLM("groq", "openai/gpt-oss-120b", "gsk-test", "https://api.groq.com/openai/v1")
CLAUDE = ResolvedLLM("anthropic", "claude-haiku-5-5", "sk-ant-test", None)
_REQUEST = httpx2.Request("POST", "https://llm.invalid/v1/chat/completions")


class Answer(BaseModel):
    items: list[str]


def status_error(cls, status: int, message: str = "error", headers: dict | None = None):
    response = httpx2.Response(status, request=_REQUEST, headers=headers or {})
    return cls(message, response=response, body=None)


class FakeOpenAI:
    """Stands in for `openai.OpenAI`: returns or raises the queued replies in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        message = SimpleNamespace(content=reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeAnthropic:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])


def groq_client(*replies, cache=None):
    fake = FakeOpenAI(*replies)
    sleeps: list[float] = []
    client = LLMClient(
        GROQ, backend=OpenAICompatBackend(GROQ, fake), cache=cache, sleep=sleeps.append
    )
    return client, fake, sleeps


# --- Settings resolution ---------------------------------------------------------


def test_no_keys_means_no_llm():
    assert resolve_llm(LLMSettings(), env={}) is None
    assert build_client(LLMSettings(), env={}) is None


def test_auto_detect_prefers_groq_then_gemini():
    both = {"GROQ_API_KEY": "g", "GEMINI_API_KEY": "m"}
    assert resolve_llm(LLMSettings(), env=both).provider == "groq"
    gemini = resolve_llm(LLMSettings(), env={"GEMINI_API_KEY": "m"})
    assert gemini.provider == "gemini"
    assert gemini.model == "gemini-2.5-flash"
    assert "generativelanguage.googleapis.com" in gemini.base_url


def test_groq_defaults():
    resolved = resolve_llm(LLMSettings(), env={"GROQ_API_KEY": "gsk"})
    assert resolved == ResolvedLLM(
        "groq", "openai/gpt-oss-120b", "gsk", "https://api.groq.com/openai/v1"
    )
    assert resolved.label == "groq/openai/gpt-oss-120b"


def test_env_beats_config():
    env = {"REVIEW_MAP_LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "m", "GROQ_API_KEY": "g"}
    resolved = resolve_llm(LLMSettings(provider="groq", model="llama-x"), env=env)
    # The config's model was written for groq, so it doesn't carry over to gemini.
    assert (resolved.provider, resolved.model) == ("gemini", "gemini-2.5-flash")


def test_config_provider_and_model():
    resolved = resolve_llm(
        LLMSettings(provider="openai", model="gpt-x"), env={"OPENAI_API_KEY": "sk"}
    )
    assert (resolved.provider, resolved.model, resolved.base_url) == ("openai", "gpt-x", None)


def test_env_model_and_base_url_override():
    env = {
        "GROQ_API_KEY": "g",
        "REVIEW_MAP_LLM_MODEL": "qwen/qwen3.8-27b",
        "REVIEW_MAP_LLM_BASE_URL": "https://proxy.invalid/v1",
    }
    resolved = resolve_llm(LLMSettings(), env=env)
    assert resolved.model == "qwen/qwen3.8-27b"
    assert resolved.base_url == "https://proxy.invalid/v1"


def test_ollama_needs_no_key():
    resolved = resolve_llm(LLMSettings(), env={"REVIEW_MAP_LLM_PROVIDER": "ollama"})
    assert resolved.api_key is None
    assert resolved.base_url == "http://localhost:11434/v1"


def test_ollama_is_never_auto_detected():
    assert resolve_llm(LLMSettings(), env={"REVIEW_MAP_LLM_BASE_URL": "http://x"}) is None


def test_explicit_provider_without_key_is_an_error():
    # The state right after copying .env.example without filling in the key.
    env = {"REVIEW_MAP_LLM_PROVIDER": "groq", "GROQ_API_KEY": ""}
    with pytest.raises(LLMError, match="GROQ_API_KEY is not set") as info:
        resolve_llm(LLMSettings(), env=env)
    assert ".env" in info.value.hint


def test_unknown_provider_is_an_error():
    with pytest.raises(LLMError, match="not a supported provider") as info:
        resolve_llm(LLMSettings(), env={"REVIEW_MAP_LLM_PROVIDER": "skynet"})
    assert "groq" in info.value.hint


def test_provider_name_is_case_insensitive():
    env = {"REVIEW_MAP_LLM_PROVIDER": " Groq ", "GROQ_API_KEY": "g"}
    assert resolve_llm(LLMSettings(), env=env).provider == "groq"


def test_make_backend_picks_sdk():
    assert isinstance(llm.make_backend(GROQ), OpenAICompatBackend)
    assert isinstance(llm.make_backend(CLAUDE), AnthropicBackend)


def test_real_sdk_clients_have_retries_off_and_a_timeout():
    groq = OpenAICompatBackend(GROQ).client
    claude = AnthropicBackend(CLAUDE).client
    assert groq.max_retries == 0 and claude.max_retries == 0
    assert str(groq.base_url).startswith("https://api.groq.com/openai/v1")
    assert groq.timeout == llm.TIMEOUT_SECONDS


# --- OpenAI-compatible backend ------------------------------------------------


def test_valid_json_reply():
    client, fake, _ = groq_client('{"items": ["a", "b"]}')
    result = client.complete_json("system says JSON", "prompt", Answer)
    assert result.value.items == ["a", "b"]
    assert result.cached is False
    call = fake.calls[0]
    assert call["model"] == "openai/gpt-oss-120b"
    assert call["response_format"] == {"type": "json_object"}  # JSON mode, not strict schema
    assert call["temperature"] == 0
    assert call["messages"][0] == {"role": "system", "content": "system says JSON"}
    assert call["messages"][1] == {"role": "user", "content": "prompt"}


def test_invalid_json_gets_one_repair_request():
    client, fake, _ = groq_client("not json at all", '{"items": ["ok"]}')
    assert client.complete_json("s", "prompt", Answer).value.items == ["ok"]
    repair = fake.calls[1]["messages"][1]["content"]
    assert repair.startswith("prompt")
    assert "no JSON object" in repair


def test_schema_mismatch_gets_one_repair_request_quoting_the_error():
    client, fake, _ = groq_client('{"items": "a"}', '{"items": ["a"]}')
    assert client.complete_json("s", "p", Answer).value.items == ["a"]
    assert "items" in fake.calls[1]["messages"][1]["content"]
    assert "did not match the schema" in fake.calls[1]["messages"][1]["content"]


def test_invalid_output_twice_is_an_error():
    client, fake, _ = groq_client('{"items": 1}', '{"wrong": []}')
    with pytest.raises(LLMError, match="unusable output twice"):
        client.complete_json("s", "p", Answer)
    assert len(fake.calls) == 2


def test_empty_reply_is_invalid_output():
    client, _, _ = groq_client("", '{"items": []}')
    assert client.complete_json("s", "p", Answer).value.items == []


def test_groq_json_validate_failed_counts_as_invalid_output():
    error = status_error(openai.BadRequestError, 400, "json_validate_failed: bad JSON")
    client, fake, _ = groq_client(error, '{"items": ["x"]}')
    assert client.complete_json("s", "p", Answer).value.items == ["x"]
    assert len(fake.calls) == 2


def test_rate_limit_is_retried_with_backoff():
    error = status_error(openai.RateLimitError, 429)
    client, fake, sleeps = groq_client(error, error, '{"items": []}')
    client.complete_json("s", "p", Answer)
    assert len(fake.calls) == 3
    assert len(sleeps) == 2
    assert 1 <= sleeps[0] < 1.5 and 2 <= sleeps[1] < 2.5  # exponential, with jitter


def test_retry_after_header_is_respected_but_capped():
    slow = status_error(openai.RateLimitError, 429, headers={"retry-after": "7"})
    huge = status_error(openai.RateLimitError, 429, headers={"retry-after": "3600"})
    client, _, sleeps = groq_client(slow, huge, '{"items": []}')
    client.complete_json("s", "p", Answer)
    assert sleeps[0] == 7
    assert sleeps[1] == llm.MAX_RETRY_AFTER_SECONDS


@pytest.mark.parametrize(
    "error",
    [
        openai.APITimeoutError(request=_REQUEST),
        openai.APIConnectionError(request=_REQUEST),
        status_error(openai.InternalServerError, 503),
    ],
    ids=["timeout", "connection", "server-error"],
)
def test_transient_errors_are_retried(error):
    client, fake, _ = groq_client(error, '{"items": ["ok"]}')
    assert client.complete_json("s", "p", Answer).value.items == ["ok"]
    assert len(fake.calls) == 2


def test_gives_up_after_max_retries():
    errors = [status_error(openai.RateLimitError, 429)] * (llm.MAX_TRANSIENT_RETRIES + 1)
    client, fake, sleeps = groq_client(*errors)
    with pytest.raises(LLMError, match="HTTP 429.*gave up after 3 retries") as info:
        client.complete_json("s", "p", Answer)
    assert len(fake.calls) == 4
    assert len(sleeps) == 3
    assert info.value.hint


def test_timeout_message_names_the_provider():
    errors = [openai.APITimeoutError(request=_REQUEST)] * 4
    client, _, _ = groq_client(*errors)
    with pytest.raises(LLMError, match="groq timed out"):
        client.complete_json("s", "p", Answer)


def test_bad_key_is_not_retried():
    client, fake, sleeps = groq_client(status_error(openai.AuthenticationError, 401))
    with pytest.raises(LLMError, match="rejected the API key") as info:
        client.complete_json("s", "p", Answer)
    assert len(fake.calls) == 1 and sleeps == []
    assert "GROQ_API_KEY" in info.value.hint
    assert "gsk-test" not in info.value.message  # never echo the key


def test_unknown_model():
    client, _, _ = groq_client(status_error(openai.NotFoundError, 404))
    with pytest.raises(LLMError, match="no model named 'openai/gpt-oss-120b'"):
        client.complete_json("s", "p", Answer)


def test_other_http_errors_are_reported():
    client, _, _ = groq_client(status_error(openai.BadRequestError, 400, "context too long"))
    with pytest.raises(LLMError, match="HTTP 400.*context too long"):
        client.complete_json("s", "p", Answer)


# --- Anthropic backend -------------------------------------------------------


def test_anthropic_backend():
    fake = FakeAnthropic('Here you go:\n```json\n{"items": ["a"]}\n```')
    client = LLMClient(CLAUDE, backend=AnthropicBackend(CLAUDE, fake))
    assert client.complete_json("sys", "prompt", Answer).value.items == ["a"]
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "prompt"}]
    assert call["max_tokens"] > 0


def test_anthropic_errors_are_classified():
    overloaded = status_error(anthropic.InternalServerError, 529)
    bad_key = status_error(anthropic.AuthenticationError, 401)
    fake = FakeAnthropic(overloaded, bad_key)
    client = LLMClient(CLAUDE, backend=AnthropicBackend(CLAUDE, fake), sleep=lambda _: None)
    with pytest.raises(LLMError, match="anthropic rejected the API key") as info:
        client.complete_json("s", "p", Answer)
    assert "ANTHROPIC_API_KEY" in info.value.hint
    assert len(fake.calls) == 2  # 529 overloaded was retried


def test_anthropic_empty_reply():
    fake = FakeAnthropic("", '{"items": []}')
    client = LLMClient(CLAUDE, backend=AnthropicBackend(CLAUDE, fake))
    assert client.complete_json("s", "p", Answer).value.items == []


# --- JSON extraction -----------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! {"a": 1} Hope that helps.', '\n```\n{"a": 1}```'],
)
def test_extract_json(reply):
    assert json.loads(extract_json(reply)) == {"a": 1}


@pytest.mark.parametrize("reply", ["", "no braces", "} backwards {"])
def test_extract_json_without_object(reply):
    with pytest.raises(llm.InvalidOutputError):
        extract_json(reply)


# --- Cache ---------------------------------------------------------------------


def test_cache_hit_skips_the_api(tmp_path):
    cache = DiskCache(tmp_path)
    first, fake, _ = groq_client('{"items": ["a"]}', cache=cache)
    assert first.complete_json("s", "p", Answer).cached is False

    second, fake2, _ = groq_client(cache=cache)  # no replies queued: a call would fail
    result = second.complete_json("s", "p", Answer)
    assert result.cached is True
    assert result.value.items == ["a"]
    assert fake2.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "other prompt"},
        {"system": "other system"},
        {"settings": ResolvedLLM("groq", "qwen/qwen3.8-27b", "k", GROQ.base_url)},
    ],
    ids=["prompt", "system", "model"],
)
def test_cache_key_covers_inputs(tmp_path, change):
    cache = DiskCache(tmp_path)
    groq_client('{"items": ["a"]}', cache=cache)[0].complete_json("s", "p", Answer)

    settings = change.get("settings", GROQ)
    fake = FakeOpenAI('{"items": ["b"]}')
    client = LLMClient(settings, backend=OpenAICompatBackend(settings, fake), cache=cache)
    result = client.complete_json(change.get("system", "s"), change.get("prompt", "p"), Answer)
    assert result.cached is False
    assert result.value.items == ["b"]


def test_invalid_answers_are_not_cached(tmp_path):
    cache = DiskCache(tmp_path)
    client, _, _ = groq_client("junk", "junk", cache=cache)
    with pytest.raises(LLMError):
        client.complete_json("s", "p", Answer)
    assert list(tmp_path.iterdir()) == []


def test_corrupt_cache_entry_is_ignored(tmp_path):
    cache = DiskCache(tmp_path)
    groq_client('{"items": ["a"]}', cache=cache)[0].complete_json("s", "p", Answer)
    (entry,) = tmp_path.iterdir()
    entry.write_text("{corrupt")

    client, fake, _ = groq_client('{"items": ["fresh"]}', cache=cache)
    result = client.complete_json("s", "p", Answer)
    assert result.value.items == ["fresh"]
    assert result.cached is False
    assert json.loads(entry.read_text()) == {"items": ["fresh"]}


def test_unwritable_cache_is_skipped(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    client, _, _ = groq_client('{"items": ["a"]}', cache=DiskCache(blocker / "cache"))
    assert client.complete_json("s", "p", Answer).value.items == ["a"]


def test_default_cache_dir(tmp_path):
    assert default_cache_dir({"REVIEW_MAP_CACHE_DIR": str(tmp_path)}) == tmp_path
    assert default_cache_dir({"XDG_CACHE_HOME": "/xdg"}).as_posix() == "/xdg/review-map/llm"
    assert default_cache_dir({}).parts[-3:] == (".cache", "review-map", "llm")


def test_build_client_cache_toggle(tmp_path):
    env = {"GROQ_API_KEY": "g", "REVIEW_MAP_CACHE_DIR": str(tmp_path)}
    assert build_client(LLMSettings(), env=env).cache.directory == tmp_path
    assert build_client(LLMSettings(), env=env, use_cache=False).cache is None


def test_network_is_blocked_in_tests():
    # Guard check: a real SDK client (no fake) must not reach the internet from tests.
    client = LLMClient(GROQ, sleep=lambda _: None)
    with pytest.raises((LLMError, RuntimeError)) as info:
        client.complete_json("s", "p", Answer)
    assert "network" in str(info.value) or "could not reach groq" in str(info.value)

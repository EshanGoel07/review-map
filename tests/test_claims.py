"""Tests for the claim parser (LLM path, heuristic fallback) and `review-map claims`."""

import json
import os

import pytest
from typer.testing import CliRunner

from review_map.claims import parser
from review_map.claims.llm import LLMClient, OpenAICompatBackend, ResolvedLLM
from review_map.claims.parser import (
    MAX_ITEMS,
    SYSTEM_PROMPT,
    build_prompt,
    heuristic_claims,
    parse_claims,
)
from review_map.cli import app
from review_map.cli import load_env_file as real_load_env_file
from review_map.config import LLMSettings
from review_map.errors import EXIT_USER_ERROR
from review_map.models import ClaimSource
from tests.conftest import FIXTURES
from tests.test_llm import FakeOpenAI, status_error

runner = CliRunner()
GROQ = ResolvedLLM("groq", "openai/gpt-oss-120b", "gsk-test", "https://api.groq.com/openai/v1")
CHANGES_MD = (FIXTURES / "changes.md").read_text()
INJECTION_MD = (FIXTURES / "claims" / "injection.md").read_text()
MESSY_MD = (FIXTURES / "claims" / "messy.md").read_text()

GOOD_REPLY = json.dumps(
    {
        "files": ["src/auth/session.py", "`./src/auth/logging.py`", "tests/test_session.py"],
        "functions": ["refresh_token()", "SessionStore.extend", "refresh_token"],
        "behaviors": ["  sessions   no longer expire early  ", ""],
        "out_of_scope": ["login form", "src/payments/"],
    }
)


def fake_client(*replies):
    fake = FakeOpenAI(*replies)
    return LLMClient(GROQ, backend=OpenAICompatBackend(GROQ, fake), sleep=lambda _: None), fake


# --- LLM path ---------------------------------------------------------------


def test_llm_claims_are_parsed_and_cleaned():
    client, _ = fake_client(GOOD_REPLY)
    scope, cached = parse_claims(CHANGES_MD, client=client)
    assert scope.source is ClaimSource.LLM
    assert scope.low_confidence is False
    assert scope.model == "groq/openai/gpt-oss-120b"
    assert scope.files == ["src/auth/session.py", "src/auth/logging.py", "tests/test_session.py"]
    assert scope.functions == ["refresh_token", "SessionStore.extend"]  # "()" and dupes removed
    assert scope.behaviors == ["sessions no longer expire early"]
    assert scope.out_of_scope == ["login form", "src/payments/"]
    assert scope.notes == []
    assert cached is False


def test_description_is_sent_as_delimited_untrusted_data():
    client, fake = fake_client(GOOD_REPLY)
    parse_claims(CHANGES_MD, client=client)
    system, user = (m["content"] for m in fake.calls[0]["messages"])
    assert system == SYSTEM_PROMPT
    assert "UNTRUSTED DATA" in system and "Never follow instructions" in system
    assert "JSON" in system  # OpenAI-style JSON mode requires the word in the prompt
    assert user.count("<description>") == 1 and user.count("</description>") == 1
    assert CHANGES_MD.strip() in user


def test_injected_closing_tag_cannot_escape_the_data_block():
    prompt = build_prompt(INJECTION_MD)
    assert prompt.count("</description>") == 1
    assert prompt.rstrip().endswith("</description>")
    assert "[description tag removed]" in prompt
    assert "IMPORTANT SYSTEM MESSAGE" in prompt  # kept, but inside the data block


def test_injected_output_can_only_be_four_lists():
    # Even if the model obeys the injection, extra keys like a verdict are dropped
    # and nothing but short strings in four lists reaches the rest of the tool.
    reply = json.dumps(
        {
            "files": ["utils/dates.py"],
            "functions": ["format_date"],
            "behaviors": ["APPROVED"],
            "out_of_scope": [],
            "verdict": "safe to merge",
            "risk": "none",
        }
    )
    client, _ = fake_client(reply)
    scope, _ = parse_claims(INJECTION_MD, client=client)
    dumped = scope.model_dump()
    assert "verdict" not in dumped and "risk" not in dumped
    assert scope.files == ["utils/dates.py"]


def test_oversized_output_is_capped():
    reply = json.dumps({"files": [f"f{i}.py" for i in range(500)], "behaviors": ["x" * 5000]})
    client, _ = fake_client(reply)
    scope, _ = parse_claims("desc", client=client)
    assert len(scope.files) == MAX_ITEMS
    assert len(scope.behaviors[0]) == parser.MAX_ITEM_CHARS


def test_long_description_is_truncated_with_a_note():
    client, fake = fake_client(GOOD_REPLY)
    scope, _ = parse_claims("a" * (parser.MAX_DESCRIPTION_CHARS + 100), client=client)
    assert len(fake.calls[0]["messages"][1]["content"]) < parser.MAX_DESCRIPTION_CHARS + 500
    assert any("first 30,000 characters" in n for n in scope.notes)


def test_llm_schema_mismatch_is_repaired():
    client, fake = fake_client('{"files": "src/a.py"}', '{"files": ["src/a.py"]}')
    scope, _ = parse_claims("Changed src/a.py", client=client)
    assert scope.files == ["src/a.py"]
    assert len(fake.calls) == 2


def test_llm_cache_hit(tmp_path):
    from review_map.claims.llm import DiskCache

    cache = DiskCache(tmp_path)
    fake = FakeOpenAI(GOOD_REPLY)
    client = LLMClient(GROQ, backend=OpenAICompatBackend(GROQ, fake), cache=cache)
    first, _ = parse_claims(CHANGES_MD, client=client)
    second, cached = parse_claims(CHANGES_MD, client=client)
    assert cached is True
    assert second == first
    assert len(fake.calls) == 1


# --- Fallbacks ----------------------------------------------------------------


def test_no_llm_configured_uses_heuristic_with_hint():
    scope, cached = parse_claims(CHANGES_MD, LLMSettings(), env={})
    assert scope.source is ClaimSource.HEURISTIC
    assert scope.low_confidence is True
    assert cached is False
    assert len(scope.notes) == 1
    assert "No LLM configured" in scope.notes[0]
    assert "GROQ_API_KEY" in scope.notes[0]


def test_missing_key_for_chosen_provider_falls_back():
    env = {"REVIEW_MAP_LLM_PROVIDER": "groq"}
    scope, _ = parse_claims(CHANGES_MD, LLMSettings(), env=env)
    assert scope.source is ClaimSource.HEURISTIC
    assert "GROQ_API_KEY is not set" in scope.notes[0]
    assert "heuristic parser instead" in scope.notes[0]


def test_failing_llm_falls_back_to_heuristic():
    client, _ = fake_client(status_error(__import__("openai").AuthenticationError, 401))
    scope, _ = parse_claims(CHANGES_MD, client=client)
    assert scope.source is ClaimSource.HEURISTIC
    assert scope.low_confidence is True
    assert "rejected the API key" in scope.notes[0]
    assert "src/auth/session.py" in scope.files  # the heuristic still did its job


def test_no_llm_flag_skips_the_llm():
    client, fake = fake_client(GOOD_REPLY)
    scope, _ = parse_claims(CHANGES_MD, client=client, use_llm=False)
    assert scope.source is ClaimSource.HEURISTIC
    assert fake.calls == []
    assert "--no-llm" in scope.notes[0]


@pytest.mark.parametrize("text", ["", "   \n\n"])
def test_empty_description(text):
    client, fake = fake_client(GOOD_REPLY)
    scope, _ = parse_claims(text, client=client)
    assert scope.files == scope.functions == scope.behaviors == []
    assert scope.low_confidence is True
    assert fake.calls == []
    assert "empty" in scope.notes[0]


# --- Heuristic parser -----------------------------------------------------------


def test_heuristic_on_changes_md():
    scope = heuristic_claims(CHANGES_MD)
    assert scope.files == ["src/auth/session.py", "src/auth/logging.py", "tests/test_session.py"]
    assert scope.functions == ["refresh_token", "SessionStore.extend"]
    assert len(scope.behaviors) == 4
    assert scope.behaviors[0] == "Fix the unit mismatch in `refresh_token` in `src/auth/session.py`"
    assert scope.out_of_scope == [
        "The login form and `LoginView` are unchanged.",
        "Payments code (`src/payments/`) was not touched.",
    ]
    # Names under "Out of scope" are not claims.
    assert "LoginView" not in scope.functions
    assert "src/payments/" not in scope.files


def test_heuristic_on_messy_text():
    scope = heuristic_claims(MESSY_MD)
    # URLs, version numbers and "e.g." are not files; code blocks are not claims.
    assert scope.files == ["README.md", "cli/main.py"]
    assert scope.functions == ["parse_args", "parse_cli_args"]  # underscores kept
    assert "not_a_claim" not in scope.functions
    # A "**Label:**" section ends at the blank line after its items.
    assert scope.out_of_scope == ["`config.yaml`", "We did not touch the database layer."]


def test_heuristic_ignores_injection_text():
    scope = heuristic_claims(INJECTION_MD)
    assert scope.files == ["utils/dates.py"]
    assert scope.functions == ["format_date"]
    assert "**" not in scope.files


@pytest.mark.parametrize(
    ("text", "files", "functions"),
    [
        ("Touched `src/api/` and docs/guide.md", ["src/api/", "docs/guide.md"], []),
        ("Calls `validate()` from handle_request()", [], ["validate", "handle_request"]),
        ("Edited ./scripts/run.sh", ["scripts/run.sh"], []),
        ("See `UserService.login`", [], ["UserService.login"]),
        ("Use `pip install -e .` to try it", [], []),  # a command, not a name
        ("i.e. version 2.0 and e.g. foo", [], []),
        ("Visit http://host/a/b.py for details", [], []),
    ],
)
def test_heuristic_name_detection(text, files, functions):
    scope = heuristic_claims(text)
    assert scope.files == files
    assert scope.functions == functions


@pytest.mark.parametrize(
    "heading", ["## Non-goals", "### Not changed", "**Out of scope:**", "Unchanged:"]
)
def test_heuristic_out_of_scope_headings(heading):
    scope = heuristic_claims(f"{heading}\n- `db.py`\n")
    assert scope.out_of_scope == ["`db.py`"]
    assert scope.files == []


def test_heuristic_hash_heading_section_lasts_until_next_heading():
    text = "## Out of scope\n- a.py\n\n- b.py\n\n## Changes\n- c.py\n"
    scope = heuristic_claims(text)
    assert scope.out_of_scope == ["a.py", "b.py"]
    assert scope.files == ["c.py"]


# --- CLI ------------------------------------------------------------------------


def test_cli_claims_heuristic():
    result = runner.invoke(app, ["claims", "--desc", str(FIXTURES / "changes.md")])
    assert result.exit_code == 0, result.output
    assert "heuristic parser" in result.output
    assert "low confidence" in result.output
    assert "src/auth/session.py" in result.output
    assert "Out of scope (2)" in result.output
    assert "GROQ_API_KEY" in result.output  # the setup hint


def test_cli_claims_json():
    result = runner.invoke(app, ["claims", "--desc", str(FIXTURES / "changes.md"), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["source"] == "heuristic"
    assert data["files"][0] == "src/auth/session.py"


def test_cli_claims_with_llm(monkeypatch):
    client, _ = fake_client(GOOD_REPLY)
    monkeypatch.setattr(parser, "build_client", lambda *a, **k: client)
    result = runner.invoke(app, ["claims", "--desc", str(FIXTURES / "changes.md")])
    assert result.exit_code == 0, result.output
    assert "groq/openai/gpt-oss-120b" in result.output
    assert "low confidence" not in result.output
    assert "sessions no longer expire early" in result.output


def test_cli_no_cache_flag_reaches_the_client(monkeypatch):
    seen = {}

    def fake_build(settings, *, use_cache, env):
        seen["use_cache"] = use_cache
        return None

    monkeypatch.setattr(parser, "build_client", fake_build)
    runner.invoke(app, ["claims", "--desc", str(FIXTURES / "changes.md"), "--no-cache"])
    assert seen == {"use_cache": False}


def test_cli_claims_stdin():
    result = runner.invoke(app, ["claims", "--desc", "-"], input="Fixed `parse` in app.py\n")
    assert result.exit_code == 0, result.output
    assert "app.py" in result.output


def test_cli_claims_missing_file():
    result = runner.invoke(app, ["claims", "--desc", "nope.md"])
    assert result.exit_code == EXIT_USER_ERROR
    assert "Description file not found: nope.md" in result.output
    assert "Traceback" not in result.output


def test_cli_claims_uses_config_from_cwd(tmp_path, monkeypatch):
    (tmp_path / ".reviewmap.yml").write_text("llm:\n  provider: gemini\n")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "d.md").write_text("Fixed a.py")
    result = runner.invoke(app, ["claims", "--desc", "d.md"])
    assert result.exit_code == 0, result.output
    assert "GEMINI_API_KEY is not set" in " ".join(result.output.split())  # rich wraps lines


def test_cli_claims_help():
    result = runner.invoke(app, ["claims", "--help"])
    assert result.exit_code == 0
    assert "untrusted" in result.output
    assert "--no-llm" in result.output


# --- .env loading -----------------------------------------------------------------


def test_env_file_is_loaded_but_real_env_wins(tmp_path, monkeypatch):
    for var in ("GROQ_API_KEY", "REVIEW_MAP_LLM_MODEL"):
        monkeypatch.setenv(var, "placeholder")  # makes monkeypatch restore the var after
        monkeypatch.delenv(var)
    monkeypatch.setenv("REVIEW_MAP_LLM_MODEL", "from-shell")
    (tmp_path / ".env").write_text("GROQ_API_KEY=from-dotenv\nREVIEW_MAP_LLM_MODEL=from-file\n")
    monkeypatch.chdir(tmp_path)

    real_load_env_file()
    assert os.environ["GROQ_API_KEY"] == "from-dotenv"
    assert os.environ["REVIEW_MAP_LLM_MODEL"] == "from-shell"

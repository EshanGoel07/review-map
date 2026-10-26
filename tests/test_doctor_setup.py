"""Tests for Phase 12's first-run commands: `doctor` and `setup` (fake input, no network)."""

import stat

import pytest
from typer.testing import CliRunner

from review_map import doctor as dr
from review_map import setup_wizard as sw
from review_map.cli import app
from review_map.errors import LLMError, ReviewMapError

runner = CliRunner()
SECRET = "gsk_supersecret_value_123"


@pytest.fixture
def env(tmp_path):
    """An environment with no keys and caches inside tmp_path."""
    return {
        "REVIEW_MAP_CACHE_DIR": str(tmp_path / "cache" / "llm"),
        "REVIEW_MAP_REPO_CACHE_DIR": str(tmp_path / "cache" / "repos"),
    }


def ok_probe(resolved):
    return None


def rate_probe(token):
    return {"limit": 5000 if token else 60, "remaining": 4999 if token else 59}


def checks_for(repo, env, **kwargs):
    kwargs.setdefault("llm_probe", ok_probe)
    kwargs.setdefault("rate_probe", rate_probe)
    return {c.name: c for c in dr.run_checks(repo, env, **kwargs)}


def test_fresh_setup_is_usable_without_any_keys(git_repo, env):
    git_repo.write("a.py", "x = 1\n")
    git_repo.commit()
    checks = checks_for(git_repo.path, env)
    assert checks["git"].ok and checks["Python"].ok and checks["Config"].ok
    assert checks["LLM"].ok is None and "heuristic parser" in checks["LLM"].detail
    assert "review-map setup" in checks["LLM"].fix
    assert checks["GitHub token"].ok is None and "59/60" in checks["GitHub token"].detail
    assert checks["Cache folders"].ok
    assert checks["Risk model"].ok and "shipped default model" in checks["Risk model"].detail
    assert dr.usable(list(checks.values()))


def test_llm_key_is_tested_but_never_shown(git_repo, env):
    env["GROQ_API_KEY"] = SECRET
    seen = []
    checks = checks_for(git_repo.path, env, llm_probe=lambda r: seen.append(r.provider))
    assert seen == ["groq"]
    assert checks["LLM"].ok and "GROQ_API_KEY set, test call worked" in checks["LLM"].detail
    assert all(SECRET not in f"{c.detail} {c.fix}" for c in checks.values())


def test_rejected_llm_key_is_reported_with_a_fix_but_still_usable(git_repo, env):
    env["GROQ_API_KEY"] = SECRET

    def reject(resolved):
        raise LLMError("groq rejected the API key (HTTP 401).", hint="Check GROQ_API_KEY.")

    checks = checks_for(git_repo.path, env, llm_probe=reject)
    assert checks["LLM"].ok is False and "rejected" in checks["LLM"].detail
    assert checks["LLM"].fix == "Check GROQ_API_KEY."
    assert dr.usable(list(checks.values()))  # analyze falls back to the heuristic parser


def test_llm_network_error_is_reported(git_repo, env):
    env["GROQ_API_KEY"] = SECRET

    def boom(resolved):
        raise TimeoutError("timed out")

    assert checks_for(git_repo.path, env, llm_probe=boom)["LLM"].ok is False


def test_explicit_provider_without_key(git_repo, env):
    env["REVIEW_MAP_LLM_PROVIDER"] = "anthropic"
    llm = checks_for(git_repo.path, env)["LLM"]
    assert llm.ok is False and "ANTHROPIC_API_KEY is not set" in llm.detail


def test_offline_skips_every_network_call(git_repo, env):
    env["GROQ_API_KEY"] = SECRET
    env["GITHUB_TOKEN"] = "ghp_x"

    def fail(*_):
        raise AssertionError("network call in --offline")

    checks = checks_for(git_repo.path, env, offline=True, llm_probe=fail, rate_probe=fail)
    assert "not tested: --offline" in checks["LLM"].detail
    assert "not tested: --offline" in checks["GitHub token"].detail


def test_github_token_rate_limit_and_bad_token(git_repo, env):
    env["GITHUB_TOKEN"] = "ghp_secret"
    good = checks_for(git_repo.path, env)["GitHub token"]
    assert good.ok and "4999/5000" in good.detail and "ghp_secret" not in good.detail

    def bad(token):
        raise ReviewMapError("GitHub rejected the token (HTTP 401).", hint="Create a new one.")

    worse = checks_for(git_repo.path, env, rate_probe=bad)["GitHub token"]
    assert worse.ok is False and worse.fix == "Create a new one."


def test_invalid_config_makes_the_setup_unusable(git_repo, env):
    git_repo.write(".reviewmap.yml", "reading_budget:\n  max_units: zero\n")
    checks = checks_for(git_repo.path, env)
    assert checks[".reviewmap.yml"].ok is False and checks[".reviewmap.yml"].required
    assert not dr.usable(list(checks.values()))


def test_valid_config_file_is_named(git_repo, env):
    git_repo.write(".reviewmap.yml", "ignore: []\n")
    assert checks_for(git_repo.path, env)[".reviewmap.yml"].ok


def test_not_a_git_repo_is_a_note_not_a_failure(tmp_path, env):
    folder = tmp_path / "plain"
    folder.mkdir()
    checks = checks_for(folder, env)
    assert checks["Config"].ok is None and "not a git repo" in checks["Config"].detail
    assert dr.usable(list(checks.values()))


def test_missing_git_and_old_python(monkeypatch):
    monkeypatch.setattr(dr.shutil, "which", lambda name: None)
    git = dr.check_git()
    assert git.ok is False and git.required and "git-scm.com" in git.fix
    old = dr.check_python((3, 10, 4))
    assert old.ok is False and "3.11" in old.detail


def test_unwritable_cache_is_reported(git_repo, env, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    env["REVIEW_MAP_CACHE_DIR"] = str(blocker / "llm")
    cache = checks_for(git_repo.path, env)["Cache folders"]
    assert cache.ok is False and "REVIEW_MAP_CACHE_DIR" in cache.fix


def test_env_file_that_is_not_gitignored_is_flagged(git_repo, env):
    git_repo.write(".env", "GROQ_API_KEY=x\n")
    checks = checks_for(git_repo.path, env)
    assert checks[".env"].ok is False and "NOT gitignored" in checks[".env"].detail
    git_repo.write(".gitignore", ".env\n")
    assert checks_for(git_repo.path, env)[".env"].ok


def test_cli_doctor_offline(git_repo, tmp_path):
    git_repo.write("a.py", "x = 1\n")
    git_repo.commit()
    result = runner.invoke(
        app,
        ["doctor", "--repo", str(git_repo.path), "--offline"],
        env={"REVIEW_MAP_CACHE_DIR": str(tmp_path / "c"), "GROQ_API_KEY": SECRET},
    )
    assert result.exit_code == 0, result.output
    assert "✓ git" in result.output and "Ready:" in result.output
    assert SECRET not in result.output


def test_cli_doctor_exits_1_when_not_usable(git_repo, tmp_path):
    git_repo.write(".reviewmap.yml", "nonsense: true\n")
    result = runner.invoke(
        app,
        ["doctor", "--repo", str(git_repo.path), "--offline"],
        env={"REVIEW_MAP_CACHE_DIR": str(tmp_path / "c")},
    )
    assert result.exit_code == 1
    assert "✗ .reviewmap.yml" in result.output and "Not ready" in result.output


# --- setup: the .env logic ------------------------------------------------------------------


def test_plan_updates_per_provider():
    assert sw.plan_updates(sw.Answers("groq", api_key="k")) == {
        "REVIEW_MAP_LLM_PROVIDER": "groq",
        "REVIEW_MAP_LLM_MODEL": "openai/gpt-oss-120b",
        "GROQ_API_KEY": "k",
    }
    ollama = sw.plan_updates(sw.Answers("ollama", model="qwen3:8b"))
    assert ollama == {"REVIEW_MAP_LLM_PROVIDER": "ollama", "REVIEW_MAP_LLM_MODEL": "qwen3:8b"}
    assert sw.plan_updates(sw.Answers("none", github_token="t")) == {"GITHUB_TOKEN": "t"}
    assert sw.plan_updates(sw.Answers("none")) == {}


def test_update_env_text_keeps_other_lines_and_replaces_in_place():
    text = (
        "# my settings\nOTHER=1\nGROQ_API_KEY=old\n# GEMINI_API_KEY=commented\nGROQ_API_KEY=dup\n"
    )
    out = sw.update_env_text(text, {"GROQ_API_KEY": "new", "GITHUB_TOKEN": "t"})
    lines = out.splitlines()
    assert lines[:3] == ["# my settings", "OTHER=1", "GROQ_API_KEY=new"]
    assert "# GEMINI_API_KEY=commented" in lines
    assert lines[4].startswith("# GROQ_API_KEY=dup")  # a later duplicate would win: disabled
    assert lines[-1] == "GITHUB_TOKEN=t" and "# Added by review-map setup" in lines
    assert sw.read_values(out) == {"OTHER": "1", "GROQ_API_KEY": "new", "GITHUB_TOKEN": "t"}


def test_conflicts_only_for_different_non_empty_values():
    text = "GROQ_API_KEY=old\nGITHUB_TOKEN=\nREVIEW_MAP_LLM_PROVIDER=groq\n"
    updates = {"GROQ_API_KEY": "new", "GITHUB_TOKEN": "t", "REVIEW_MAP_LLM_PROVIDER": "groq"}
    assert sw.conflicts(text, updates) == ["GROQ_API_KEY"]


# --- setup: the wizard --------------------------------------------------------------------


def wizard(folder, answers: list[str], *args: str):
    return runner.invoke(
        app,
        ["setup", "--env-file", str(folder / ".env"), *args],
        input="\n".join(answers) + "\n",
    )


def test_wizard_writes_a_private_env_file_and_never_echoes_the_key(git_repo):
    git_repo.write(".gitignore", ".env\n")
    # choice 1 (groq), key, default model, no GitHub token, don't run doctor
    result = wizard(git_repo.path, ["1", SECRET, "", "", "n"])
    assert result.exit_code == 0, result.output
    env_file = git_repo.path / ".env"
    values = sw.read_values(env_file.read_text())
    assert values["GROQ_API_KEY"] == SECRET
    assert values["REVIEW_MAP_LLM_PROVIDER"] == "groq"
    assert SECRET not in result.output
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "not gitignored" not in result.output


def test_wizard_asks_before_touching_an_existing_env(git_repo):
    git_repo.write(".gitignore", ".env\n")
    git_repo.write(".env", "OTHER=1\n")
    result = wizard(git_repo.path, ["2", "gem-key", "", "", "n"])  # decline the update
    assert result.exit_code == 0, result.output
    assert "Not written" in result.output
    assert (git_repo.path / ".env").read_text() == "OTHER=1\n"


def test_wizard_keeps_an_existing_key_unless_told_to_replace_it(git_repo):
    git_repo.write(".gitignore", ".env\n")
    git_repo.write(".env", "GROQ_API_KEY=keep-me\n")
    # groq, new key, default model, no token, yes update .env, NO replace, no doctor
    result = wizard(git_repo.path, ["1", "other", "", "", "y", "n", "n"])
    assert result.exit_code == 0, result.output
    values = sw.read_values((git_repo.path / ".env").read_text())
    assert values["GROQ_API_KEY"] == "keep-me"
    assert values["REVIEW_MAP_LLM_PROVIDER"] == "groq"


def test_wizard_warns_and_fixes_gitignore(git_repo):
    # groq, key, model, no token, yes add to .gitignore, no doctor
    result = wizard(git_repo.path, ["groq", "k", "", "", "y", "n"])
    assert result.exit_code == 0, result.output
    assert "is not gitignored" in result.output
    assert ".env" in (git_repo.path / ".gitignore").read_text().splitlines()


def test_wizard_with_no_llm_and_no_token_writes_nothing(git_repo):
    result = wizard(git_repo.path, ["6", "", "n"])
    assert result.exit_code == 0, result.output
    assert "Nothing to write" in result.output
    assert "heuristic parser" in result.output
    assert not (git_repo.path / ".env").exists()


def test_wizard_rejects_an_unknown_choice(git_repo):
    result = wizard(git_repo.path, ["9"])
    assert result.exit_code != 0
    assert "Unknown choice" in result.output


def test_wizard_can_run_doctor_at_the_end(git_repo, monkeypatch):
    git_repo.write(".gitignore", ".env\n")
    monkeypatch.setattr(dr, "probe_llm", ok_probe)
    calls = []
    import review_map.cli as cli

    monkeypatch.setattr(cli, "doctor", lambda **kw: calls.append(kw))
    result = wizard(git_repo.path, ["5", "", "", "y"])  # ollama, default model, no token
    assert result.exit_code == 0, result.output
    assert calls == [{"repo": cli.Path("."), "offline": False}]


def test_non_interactive_prints_the_steps():
    result = runner.invoke(app, ["setup", "--non-interactive"])
    assert result.exit_code == 0
    assert "review-map doctor" in result.output and "GROQ_API_KEY" in result.output

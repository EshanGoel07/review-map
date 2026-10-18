"""Tests for loading and validating .reviewmap.yml."""

from pathlib import Path

import pytest

from review_map.config import Config, load_config, parse_config
from review_map.errors import ConfigError


def test_missing_config_uses_defaults(tmp_path: Path):
    loaded = load_config(tmp_path)
    assert loaded.source is None
    assert loaded.config == Config()
    assert loaded.config.reading_budget.lines_fraction == 0.15
    assert loaded.config.reading_budget.max_units == 5
    assert loaded.config.llm.provider is None


def test_valid_config(tmp_path: Path):
    (tmp_path / ".reviewmap.yml").write_text(
        """
ignore:
  - "docs/"
  - "*.snap"
sensitive_paths:
  - "src/billing/"
reading_budget:
  lines_fraction: 0.2
  max_units: 8
llm:
  provider: groq
  model: llama-3.3-70b-versatile
"""
    )
    loaded = load_config(tmp_path)
    assert loaded.source == tmp_path / ".reviewmap.yml"
    cfg = loaded.config
    assert cfg.ignore == ["docs/", "*.snap"]
    assert cfg.sensitive_paths == ["src/billing/"]
    assert cfg.reading_budget.lines_fraction == 0.2
    assert cfg.reading_budget.max_units == 8
    assert cfg.llm.provider == "groq"


def test_yaml_extension_also_found(tmp_path: Path):
    (tmp_path / ".reviewmap.yaml").write_text("ignore: ['x/']\n")
    assert load_config(tmp_path).config.ignore == ["x/"]


def test_partial_config_keeps_other_defaults():
    cfg = parse_config("reading_budget:\n  max_units: 3\n")
    assert cfg.reading_budget.max_units == 3
    assert cfg.reading_budget.lines_fraction == 0.15


def test_empty_file_is_defaults():
    assert parse_config("") == Config()
    assert parse_config("# just a comment\n") == Config()


def test_explicit_path(tmp_path: Path):
    path = tmp_path / "custom.yml"
    path.write_text("ignore: ['a/']\n")
    loaded = load_config(tmp_path / "elsewhere", explicit=path)
    assert loaded.source == path
    assert loaded.config.ignore == ["a/"]


def test_explicit_path_missing(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path, explicit=tmp_path / "nope.yml")


def test_invalid_yaml_reports_line():
    # the bad line (" bad: [") is line 3
    with pytest.raises(ConfigError, match=r"not valid YAML \(line 3"):
        parse_config("ignore:\n  - a\n bad: [\n")


def test_top_level_must_be_mapping():
    with pytest.raises(ConfigError, match="must be a mapping"):
        parse_config("- just\n- a list\n")


def test_unknown_key_rejected():
    with pytest.raises(ConfigError) as exc:
        parse_config("ignore_paths: ['a/']\n")
    assert "ignore_paths" in exc.value.message
    assert exc.value.hint


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("reading_budget:\n  lines_fraction: 1.5\n", "reading_budget.lines_fraction"),
        ("reading_budget:\n  lines_fraction: 0\n", "reading_budget.lines_fraction"),
        ("reading_budget:\n  max_units: 0\n", "reading_budget.max_units"),
        ("llm:\n  provider: skynet\n", "llm.provider"),
        ("ignore: 'not-a-list'\n", "ignore"),
    ],
)
def test_invalid_values_name_the_field(text, where):
    with pytest.raises(ConfigError) as exc:
        parse_config(text)
    assert where in exc.value.message


def test_error_shows_short_filename(tmp_path: Path):
    (tmp_path / ".reviewmap.yml").write_text("reading_budget:\n  max_units: 0\n")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    assert exc.value.message.startswith(".reviewmap.yml has invalid settings")


def test_example_config_is_valid_and_matches_defaults():
    example = Path(__file__).parents[1] / "examples" / "reviewmap.yml"
    assert parse_config(example.read_text()) == Config()

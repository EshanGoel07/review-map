"""Smoke tests: the package imports and the CLI runs."""

from typer.testing import CliRunner

import review_map
from review_map.cli import app

runner = CliRunner()


def test_package_has_version() -> None:
    assert isinstance(review_map.__version__, str)
    assert review_map.__version__


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"review-map {review_map.__version__}"


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "reading plan" in result.output
    assert "version" in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_unknown_command_fails() -> None:
    result = runner.invoke(app, ["does-not-exist"])
    assert result.exit_code != 0

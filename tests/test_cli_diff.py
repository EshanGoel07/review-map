"""Tests for the `review-map diff` command."""

from typer.testing import CliRunner

from review_map.cli import app
from review_map.errors import EXIT_USER_ERROR

runner = CliRunner()


def _make_feature(git_repo):
    git_repo.write("app.py", "def f():\n    return 1\n")
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.write("app.py", "def f():\n    return 2\n")
    git_repo.write("yarn.lock", "# lock\n")
    git_repo.write("logo.png", b"\x89PNG\x00\x01")
    git_repo.commit("feature")


def test_diff_lists_files(git_repo):
    _make_feature(git_repo)
    result = runner.invoke(app, ["diff", "--repo", str(git_repo.path), "--base", "main"])
    assert result.exit_code == 0, result.output
    assert "app.py" in result.output
    assert "lockfile" in result.output
    assert "binary" in result.output
    assert "3 files changed" in result.output
    assert "1 to review" in result.output
    assert "config: defaults" in result.output


def test_diff_base_defaults_to_main(git_repo):
    _make_feature(git_repo)
    result = runner.invoke(app, ["diff", "--repo", str(git_repo.path)])
    assert result.exit_code == 0, result.output
    assert "main...HEAD" in result.output


def test_diff_uses_repo_config(git_repo):
    _make_feature(git_repo)
    git_repo.write(".reviewmap.yml", "ignore: ['app.py']\n")
    result = runner.invoke(app, ["diff", "--repo", str(git_repo.path)])
    assert result.exit_code == 0, result.output
    assert "config: .reviewmap.yml" in result.output
    assert "ignored (app.py)" in result.output


def test_diff_bad_ref_is_friendly(git_repo):
    _make_feature(git_repo)
    result = runner.invoke(app, ["diff", "--repo", str(git_repo.path), "--base", "nope"])
    assert result.exit_code == EXIT_USER_ERROR
    assert "Unknown git ref: 'nope'" in result.output
    assert "Traceback" not in result.output


def test_diff_not_a_repo(tmp_path):
    result = runner.invoke(app, ["diff", "--repo", str(tmp_path)])
    assert result.exit_code == EXIT_USER_ERROR
    assert "Not a git repository" in result.output


def test_diff_bad_config(git_repo):
    _make_feature(git_repo)
    git_repo.write(".reviewmap.yml", "reading_budget:\n  max_units: 0\n")
    result = runner.invoke(app, ["diff", "--repo", str(git_repo.path)])
    assert result.exit_code == EXIT_USER_ERROR
    assert "reading_budget.max_units" in result.output


def test_diff_no_changes(git_repo):
    git_repo.write("a.py", "a = 1\n")
    git_repo.commit("base")
    result = runner.invoke(app, ["diff", "--repo", str(git_repo.path), "--head", "main"])
    assert result.exit_code == 0
    assert "No changes" in result.output

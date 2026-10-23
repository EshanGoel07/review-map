"""Tests for reading diffs from a real (temporary, local) git repo."""

from pathlib import Path

import pytest

from review_map.diff import git_source
from review_map.diff.parser import parse_diff
from review_map.errors import GitError


@pytest.fixture
def feature_repo(git_repo):
    git_repo.write("app.py", "def f():\n    return 1\n")
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.write("app.py", "def f():\n    return 2\n")
    git_repo.commit("feature change")
    return git_repo


def test_find_repo_root_from_subdir(feature_repo):
    sub = feature_repo.path / "pkg"
    sub.mkdir()
    assert git_source.find_repo_root(sub) == feature_repo.path.resolve()


def test_not_a_repo(tmp_path: Path):
    with pytest.raises(GitError, match="Not a git repository"):
        git_source.find_repo_root(tmp_path)


def test_path_missing(tmp_path: Path):
    with pytest.raises(GitError, match="does not exist"):
        git_source.find_repo_root(tmp_path / "nope")


def test_unknown_ref(feature_repo):
    with pytest.raises(GitError, match="Unknown git ref: 'nope'") as exc:
        git_source.resolve_ref(feature_repo.path, "nope")
    assert exc.value.hint


def test_ref_that_looks_like_option_rejected(feature_repo):
    with pytest.raises(GitError, match="Invalid ref"):
        git_source.resolve_ref(feature_repo.path, "--output=/tmp/x")


def test_empty_repo_hint(git_repo):
    with pytest.raises(GitError) as exc:
        git_source.resolve_ref(git_repo.path, "HEAD")
    assert "no commits" in exc.value.hint


def test_diff_between_branches(feature_repo):
    text = git_source.get_diff(feature_repo.path, "main", "feature")
    (f,) = parse_diff(text)
    assert f.path == "app.py"
    assert (f.added, f.removed) == (1, 1)


def test_three_dot_ignores_new_commits_on_base(feature_repo):
    """Commits added to main after branching must not appear in the PR diff."""
    feature_repo.git("checkout", "-q", "main")
    feature_repo.write("other.py", "x = 1\n")
    feature_repo.commit("unrelated work on main")
    files = parse_diff(git_source.get_diff(feature_repo.path, "main", "feature"))
    assert [f.path for f in files] == ["app.py"]


def test_user_git_config_does_not_change_output(feature_repo):
    feature_repo.git("config", "diff.noprefix", "true")
    feature_repo.git("config", "color.ui", "always")
    (f,) = parse_diff(git_source.get_diff(feature_repo.path, "main", "feature"))
    assert f.path == "app.py"


def test_rename_detected(git_repo):
    git_repo.write("old.py", "".join(f"line{i} = {i}\n" for i in range(20)))
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.git("mv", "old.py", "new.py")
    git_repo.commit("rename")
    (f,) = parse_diff(git_source.get_diff(git_repo.path, "main", "feature"))
    assert f.status == "renamed"
    assert f.old_path == "old.py"


def test_no_merge_base(git_repo):
    git_repo.write("a.py", "a = 1\n")
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "--orphan", "other")
    git_repo.write("b.py", "b = 1\n")
    git_repo.commit("unrelated root")
    with pytest.raises(GitError, match="no common history"):
        git_source.get_diff(git_repo.path, "main", "other")


def test_default_base_prefers_main(feature_repo):
    assert git_source.default_base(feature_repo.path) == "main"


def test_default_base_falls_back_to_master(git_repo):
    git_repo.git("checkout", "-q", "-b", "master")
    git_repo.write("a.py", "a = 1\n")
    git_repo.commit("base")
    assert git_source.default_base(git_repo.path) == "master"


def test_default_base_none_found(git_repo):
    git_repo.git("checkout", "-q", "-b", "develop")
    git_repo.write("a.py", "a = 1\n")
    git_repo.commit("base")
    with pytest.raises(GitError, match="Could not guess the base"):
        git_source.default_base(git_repo.path)


def test_diff_keeps_lone_carriage_returns_inside_lines(git_repo):
    """Python's text mode would turn a lone "\r" into a line break and break parsing."""
    git_repo.write("a.txt", "one\n")
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    (git_repo.path / "a.txt").write_bytes(b"one\nfoo\rbar\x0cbaz\nthree\r\n")
    git_repo.commit("feature")
    text = git_source.get_diff(git_repo.path, "main", "feature")
    assert "foo\rbar" in text
    (changed,) = parse_diff(text)
    assert changed.added == 2

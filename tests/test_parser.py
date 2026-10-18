"""Tests for parsing unified diffs into ChangedFiles."""

import pytest

from review_map.config import Config
from review_map.diff.parser import detect_language, generated_reason, parse_diff
from review_map.errors import ReviewMapError
from review_map.models import FileStatus, LineKind

from .conftest import FIXTURES


@pytest.fixture(scope="module")
def files():
    text = (FIXTURES / "diffs" / "mixed.diff").read_text()
    return {f.path: f for f in parse_diff(text)}


def test_all_files_found(files):
    assert set(files) == {
        "added.py",
        "dir/pure_new.txt",
        "gone.py",
        "img.bin",
        "mod.py",
        "new_name.txt",
        "package-lock.json",
        "run.sh",
        "sp ace/f.py",
    }


def test_added_file(files):
    f = files["added.py"]
    assert f.status is FileStatus.ADDED
    assert f.language == "python"
    assert (f.added, f.removed) == (1, 0)
    assert f.hunks[0].added_linenos == [1]
    assert f.is_reviewable


def test_deleted_file_keeps_old_line_numbers(files):
    f = files["gone.py"]
    assert f.status is FileStatus.DELETED
    assert (f.added, f.removed) == (0, 1)
    assert f.hunks[0].removed_linenos == [1]
    assert f.hunks[0].lines[0].text == "x = 1"


def test_modified_file_line_numbers(files):
    f = files["mod.py"]
    assert f.status is FileStatus.MODIFIED
    (hunk,) = f.hunks
    assert (hunk.old_start, hunk.old_length, hunk.new_start, hunk.new_length) == (1, 6, 1, 10)
    assert hunk.removed_linenos == [2]
    assert hunk.added_linenos == [2, 7, 8, 9, 10]
    # "\ No newline at end of file" is not a code line
    assert all(not ln.text.startswith("\\") for ln in hunk.lines)
    assert hunk.lines[-1].text == "    return 3"
    context = [ln for ln in hunk.lines if ln.kind is LineKind.CONTEXT]
    assert context[0].old_lineno == 1 and context[0].new_lineno == 1


def test_rename_with_edit(files):
    f = files["new_name.txt"]
    assert f.status is FileStatus.RENAMED
    assert f.old_path == "old_name.txt"
    assert (f.added, f.removed) == (1, 1)


def test_pure_rename_has_no_hunks(files):
    f = files["dir/pure_new.txt"]
    assert f.status is FileStatus.RENAMED
    assert f.old_path == "pure_old.txt"
    assert f.hunks == []


def test_binary_file_skipped(files):
    f = files["img.bin"]
    assert f.is_binary
    assert f.hunks == []
    assert not f.is_reviewable


def test_lockfile_flagged_generated(files):
    f = files["package-lock.json"]
    assert f.generated_reason == "lockfile"
    assert not f.is_reviewable


def test_mode_change_only(files):
    f = files["run.sh"]
    assert f.status is FileStatus.MODIFIED
    assert f.hunks == []
    assert f.language == "shell"


def test_path_with_space(files):
    assert files["sp ace/f.py"].status is FileStatus.ADDED


def test_config_ignore_globs():
    text = (FIXTURES / "diffs" / "mixed.diff").read_text()
    config = Config(ignore=["*.txt", "sp ace/"])
    files = {f.path: f for f in parse_diff(text, config)}
    assert files["new_name.txt"].ignored_by == "*.txt"
    assert files["dir/pure_new.txt"].ignored_by == "*.txt"
    assert files["sp ace/f.py"].ignored_by == "sp ace/"
    assert files["mod.py"].ignored_by is None


def test_empty_diff():
    assert parse_diff("") == []


def test_malformed_diff_is_friendly_error():
    bad = "--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n-only one line\n"
    with pytest.raises(ReviewMapError, match="Could not parse"):
        parse_diff(bad)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("yarn.lock", "lockfile"),
        ("frontend/pnpm-lock.yaml", "lockfile"),
        ("uv.lock", "lockfile"),
        ("dist/app.js", "build output"),
        ("packages/ui/dist/index.js", "build output"),
        ("vendor/lib/x.go", "vendored"),
        ("static/jquery.min.js", "minified"),
        ("proto/user_pb2.py", "generated code"),
        ("src/app.py", None),
        ("src/distance.py", None),
        ("docs/building.md", None),
    ],
)
def test_generated_detection(path, reason):
    assert generated_reason(path) == reason


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("a.py", "python"),
        ("a.ts", "typescript"),
        ("a.tsx", "tsx"),
        ("a.JS", "javascript"),
        ("Makefile", None),
    ],
)
def test_detect_language(path, language):
    assert detect_language(path) == language

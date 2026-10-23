"""Tests for Phase 9: fix detection, function-level SZZ and the dataset.

Each test builds a small git history in a temp folder (introduce a bug, then fix
it) and checks that SZZ blames exactly the right commit and function.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from review_map.cli import app
from review_map.config import Config
from review_map.diff.parser import parse_diff
from review_map.errors import ReviewMapError
from review_map.training import dataset
from review_map.training.dataset import CHECKPOINT, build_dataset, read_checkpoint
from review_map.training.mine import MAX_FILES, is_fix_message, list_commits, resolve_repo
from review_map.training.szz import (
    _ranges,
    carries_code,
    fixed_lines,
    is_labelable,
    parse_porcelain,
)
from tests.conftest import GitRepo

runner = CliRunner()

CALC_V1 = """\
def add(a, b):
    return a + b


def discount(price, pct):
    return price
"""

# The bug: divides by 10 instead of 100.
CALC_BUGGY = """\
def add(a, b):
    return a + b


def discount(price, pct):
    off = price * pct / 10
    return price - off
"""

CALC_FIXED = """\
def add(a, b):
    return a + b


def discount(price, pct):
    off = price * pct / 100
    return price - off
"""


def commit_at(repo: GitRepo, monkeypatch, message: str, day: int) -> str:
    """Commit with a fixed date (2024-01-01 + `day`), so days_to_fix is predictable."""
    date = (
        f"2024-01-{1 + day:02d}T12:00:00+00:00"
        if day < 31
        else f"2024-03-{day - 30:02d}T12:00:00+00:00"
    )
    monkeypatch.setenv("GIT_COMMITTER_DATE", date)
    monkeypatch.setenv("GIT_AUTHOR_DATE", date)
    return repo.commit(message)


@pytest.fixture
def history(git_repo, monkeypatch):
    """add + discount → bug in discount → unrelated docs → fix of the bug."""
    r = git_repo
    r.write("README.md", "# calc\n")
    r.write("calc/__init__.py", "")
    root = commit_at(r, monkeypatch, "Initial commit", 0)
    r.write("calc/core.py", CALC_V1)
    first = commit_at(r, monkeypatch, "Add add() and discount()", 1)
    r.write("calc/core.py", CALC_BUGGY)
    bug = commit_at(r, monkeypatch, "Apply percentage discounts", 2)
    r.write("README.md", "# calc\n\nA tiny calculator.\n")
    docs = commit_at(r, monkeypatch, "Describe the project", 3)
    r.write("calc/core.py", CALC_FIXED)
    fix = commit_at(r, monkeypatch, "Fix discount percentage (#12)", 12)
    return {"repo": r, "root": root, "first": first, "bug": bug, "docs": docs, "fix": fix}


def mine(repo, tmp_path, **kwargs):
    rows, stats, head = build_dataset(repo.path, tmp_path / "work", "calc", **kwargs)
    return {(r["commit"], r["name"]): r for r in rows}, stats, rows


# --- Fix detection -------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Fix discount percentage (#12)",
        "fixes crash on empty input",
        "Fixed timeout handling",
        'Revert "Use a cached SSLContext"',
        "hotfix: wrong header",
        "Regression in redirect handling",
        "BUG: off-by-one in paginate",
        "bugfix for netrc",
    ],
)
def test_fix_messages(message):
    assert is_fix_message(message)


@pytest.mark.parametrize(
    "message",
    [
        "Add prefix handling",  # "fix" inside a word
        "Add fixture for sessions",
        "Fix typo in README",
        "docs: fix broken link",
        "Fix flake8 warnings",
        "Fix CI",
        "Improve error messages",
        "",
        "Update changelog\n\nThis fixes the release notes.",  # body ignored
    ],
)
def test_not_fix_messages(message):
    assert not is_fix_message(message)


# --- SZZ helpers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("    off = price * pct / 10", True),
        ("", False),
        ("   ", False),
        ("    )", False),
        ("    # explain the maths", False),
        ("    // js comment", False),
        ("return x  # trailing comment", True),
    ],
)
def test_carries_code(text, expected):
    assert carries_code(text) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("requests/sessions.py", True),
        ("src/app/ui.js", True),
        ("tests/test_x.py", False),
        ("docs/_static/native.js", False),
        ("pkg/docs/conf.py", False),
        ("README.md", False),
        ("doctor.py", True),  # "doc" only as a folder
    ],
)
def test_is_labelable(path, expected):
    from review_map.diff.parser import detect_language

    assert is_labelable(path, detect_language(path)) is expected


def test_ranges():
    assert _ranges([3, 4, 5, 9, 11, 12]) == [(3, 5), (9, 9), (11, 12)]
    assert _ranges([]) == []


def test_parse_porcelain_skips_boundary_lines():
    sha_a, sha_b = "a" * 40, "b" * 40
    text = (
        f"{sha_a} 7 3 1\nauthor X\nfilename calc/core.py\n\tcode\n"
        f"{sha_b} 1 4 1\nauthor Y\nboundary\nfilename old.py\n\troot code\n"
    )
    assert parse_porcelain(text) == [(sha_a, "calc/core.py", 7)]


def test_fixed_lines_ignores_tests_comments_and_blank_lines():
    diff = """\
diff --git a/calc/core.py b/calc/core.py
--- a/calc/core.py
+++ b/calc/core.py
@@ -1,4 +1,2 @@
-# old comment
-
-x = compute()
+x = compute2()
 y = 1
diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1,1 +1,1 @@
-assert x == 1
+assert x == 2
"""
    assert fixed_lines(parse_diff(diff, Config())) == {"calc/core.py": [3]}


# --- SZZ on real history ----------------------------------------------------------------


def test_szz_blames_the_commit_and_function_that_introduced_the_bug(history, tmp_path):
    by_key, stats, _ = mine(history["repo"], tmp_path)
    bug_row = by_key[(history["bug"], "discount")]
    assert bug_row["label"] == 1
    assert bug_row["fix_commits"] == history["fix"]
    assert bug_row["days_to_fix"] == pytest.approx(10.0)
    # Everything else is not risky: the earlier version, the untouched function, the fix itself.
    assert by_key[(history["first"], "discount")]["label"] == 0
    assert by_key[(history["first"], "add")]["label"] == 0
    assert by_key[(history["fix"], "discount")]["label"] == 0
    assert stats.fixes == 1
    assert stats.skipped_root == 1


def test_relative_work_dir_still_labels(history, tmp_path, monkeypatch):
    """Regression: a relative ignore-revs path made every blame fail, silently."""
    monkeypatch.chdir(tmp_path)
    rows, stats, _ = build_dataset(history["repo"].path, Path("data/work"), "calc")
    assert stats.blame_errors == 0
    assert sum(r["label"] for r in rows) == 1


def test_blame_failures_are_counted_not_hidden(history, tmp_path, monkeypatch):
    from review_map.training import szz

    monkeypatch.setattr(szz, "blame", lambda *a, **k: None)
    rows, stats, _ = build_dataset(history["repo"].path, tmp_path / "w", "calc")
    assert stats.blame_errors == 1
    assert sum(r["label"] for r in rows) == 0


def test_cli_warns_when_fixes_blame_nothing(history, tmp_path, monkeypatch):
    from review_map.training import szz

    monkeypatch.setattr(szz, "blame", lambda *a, **k: None)
    result = runner.invoke(
        app,
        ["mine", str(history["repo"].path), "--data-dir", str(tmp_path / "d")],
        env={"COLUMNS": "200"},
    )
    out = " ".join(result.output.split())
    assert "git blame failed on 1 file(s)" in out
    assert "the labels are probably wrong" in out


def test_rows_use_the_products_function_mapping(history, tmp_path):
    by_key, _, rows = mine(history["repo"], tmp_path)
    assert {r["path"] for r in rows} == {"calc/core.py"}  # README isn't code; root is skipped
    row = by_key[(history["bug"], "discount")]
    assert (row["kind"], row["start_line"], row["end_line"]) == ("function", 5, 7)
    assert (row["added"], row["removed"]) == (2, 1)
    assert row["is_fix"] is False
    assert by_key[(history["fix"], "discount")]["is_fix"] is True


def test_test_files_are_not_blamed_or_rows(git_repo, monkeypatch, tmp_path):
    r = git_repo
    r.write("README.md", "x\n")
    commit_at(r, monkeypatch, "init", 0)
    r.write("calc.py", "def f():\n    return 1\n")
    r.write("tests/test_calc.py", "def test_f():\n    assert f() == 2\n")  # the test is wrong
    added = commit_at(r, monkeypatch, "Add f", 1)
    r.write("tests/test_calc.py", "def test_f():\n    assert f() == 1\n")
    commit_at(r, monkeypatch, "Fix test_f expectation", 2)
    by_key, stats, rows = mine(r, tmp_path)
    assert {row["path"] for row in rows} == {"calc.py"}
    assert by_key[(added, "f")]["label"] == 0
    assert by_key[(added, "f")]["commit_tests_changed"] is True
    assert stats.implicated_lines == 0


def test_whitespace_and_comment_changes_in_a_fix_blame_nothing(git_repo, monkeypatch, tmp_path):
    r = git_repo
    r.write("README.md", "x\n")
    commit_at(r, monkeypatch, "init", 0)
    r.write("calc.py", "def f(x):\n    # add one\n    return x+1\n")
    added = commit_at(r, monkeypatch, "Add f", 1)
    r.write("calc.py", "def f(x):\n    # add 1\n    return  x+1\n")  # comment + spacing only
    commit_at(r, monkeypatch, "Fix f", 2)
    by_key, stats, _ = mine(r, tmp_path)
    assert by_key[(added, "f")]["label"] == 0
    assert stats.fixes == 1
    assert stats.implicated_lines == 0


def test_huge_commits_are_skipped_and_blamed_through(git_repo, monkeypatch, tmp_path):
    """A reformat touching > 50 files mustn't take the blame: blame looks through it."""
    r = git_repo
    r.write("README.md", "x\n")
    commit_at(r, monkeypatch, "init", 0)
    r.write("calc/core.py", CALC_BUGGY)
    bug = commit_at(r, monkeypatch, "Add discount", 1)
    r.write("calc/core.py", CALC_BUGGY.replace("price * pct / 10", "(price * pct) / 10"))
    for i in range(MAX_FILES):
        r.write(f"pkg/m{i}.py", f"X = {i}\n")
    reformat = commit_at(r, monkeypatch, "Reformat everything", 2)
    r.write("calc/core.py", CALC_FIXED)
    commit_at(r, monkeypatch, "Fix discount", 3)
    by_key, stats, rows = mine(r, tmp_path)
    assert stats.skipped_huge == 1
    assert not any(row["commit"] == reformat for row in rows)
    assert by_key[(bug, "discount")]["label"] == 1


def test_merge_commits_are_not_mined(git_repo, monkeypatch, tmp_path):
    r = git_repo
    r.write("calc.py", "def f():\n    return 1\n")
    commit_at(r, monkeypatch, "init", 0)
    r.git("checkout", "-q", "-b", "side")
    r.write("other.py", "def g():\n    return 2\n")
    commit_at(r, monkeypatch, "Add g", 1)
    r.git("checkout", "-q", "main")
    r.write("calc.py", "def f():\n    return 3\n")
    commit_at(r, monkeypatch, "Change f", 2)
    r.git("merge", "-q", "--no-ff", "side", "-m", "Merge branch side: fix everything")
    shas = {c.subject for c in list_commits(r.path)}
    assert "Merge branch side: fix everything" not in shas
    assert {"init", "Add g", "Change f"} == shas


def test_too_recent_flag(history, tmp_path, monkeypatch):
    monkeypatch.setattr(dataset, "RECENT_DAYS", 5)  # newest commit is day 12
    by_key, _, _ = mine(history["repo"], tmp_path)
    assert by_key[(history["fix"], "discount")]["too_recent"] is True
    assert by_key[(history["bug"], "discount")]["too_recent"] is False


# --- Resumable ------------------------------------------------------------------------------


def test_resume_continues_where_it_stopped(history, tmp_path):
    full, _, _ = build_dataset(history["repo"].path, tmp_path / "clean", "calc")

    work = tmp_path / "resumed"
    _, first, _ = build_dataset(history["repo"].path, work, "calc", stop_after=2)
    assert first.processed == 2
    # Simulate a crash mid-write: a torn last line must be ignored, not break the rerun.
    with (work / CHECKPOINT).open("a") as fh:
        fh.write('{"sha": "torn')
    rows, second, _ = build_dataset(history["repo"].path, work, "calc")
    assert second.processed == 3  # 5 commits, 2 done before
    key = lambda r: (r["commit"], r["name"])  # noqa: E731
    assert sorted(rows, key=key) == sorted(full, key=key)
    shas = [r["sha"] for r in read_checkpoint(work / CHECKPOINT)]
    assert len(shas) == len(set(shas)) == 5  # no commit mined twice


def test_one_unreadable_commit_does_not_stop_mining(history, tmp_path, monkeypatch):
    real = dataset.commit_change

    def flaky(repo, c):
        if c.sha == history["docs"]:
            raise ReviewMapError("Could not parse the diff: test")
        return real(repo, c)

    monkeypatch.setattr(dataset, "commit_change", flaky)
    by_key, stats, _ = mine(history["repo"], tmp_path)
    assert stats.skipped_error == 1
    assert by_key[(history["bug"], "discount")]["label"] == 1  # later commits still mined
    record = next(r for r in read_checkpoint(tmp_path / "work" / CHECKPOINT)
                  if r["sha"] == history["docs"])  # fmt: skip
    assert record["error"].startswith("Could not parse")


def test_lone_carriage_returns_keep_line_numbers(git_repo, monkeypatch, tmp_path):
    """Regression: stray "\r" bytes in a text-like file broke diff parsing (an .ai logo)."""
    r = git_repo
    r.write("README.md", "x\n")
    commit_at(r, monkeypatch, "init", 0)
    (r.path / "logo.ai").write_bytes(b"%PDF-1.5\rheader\x0c\x1cmore\nline two\r\nline three\n")
    r.write("calc.py", "def f():\n    return 1\n")
    added = commit_at(r, monkeypatch, "Add logo and f", 1)
    by_key, stats, _ = mine(r, tmp_path)
    assert stats.skipped_error == 0
    assert (added, "f") in by_key


def test_rerun_mines_nothing_new(history, tmp_path):
    build_dataset(history["repo"].path, tmp_path / "w", "calc")
    _, stats, _ = build_dataset(history["repo"].path, tmp_path / "w", "calc")
    assert stats.processed == 0


# --- Saving and the CLI ------------------------------------------------------------------


def test_saved_dataset_round_trips(history, tmp_path):
    _, _, rows = mine(history["repo"], tmp_path)
    frame = dataset.to_frame(rows)
    path = tmp_path / "out" / "dataset.parquet"
    dataset.save(frame, path)
    back = pd.read_parquet(path)
    assert list(back.columns) == dataset.COLUMNS
    assert len(back) == len(rows)
    assert int(back["label"].sum()) == 1
    assert str(back["date"].dt.tz) == "UTC"
    assert "added_lines" not in back.columns


def test_resolve_repo_rejects_bad_targets(tmp_path):
    with pytest.raises(ReviewMapError, match="Not a folder"):
        resolve_repo("../../etc", tmp_path)
    with pytest.raises(ReviewMapError, match="Not a folder"):
        resolve_repo("https://example.com/x", tmp_path)


def test_cli_mine(history, tmp_path):
    out = tmp_path / "ds.parquet"
    result = runner.invoke(
        app,
        [
            "mine",
            str(history["repo"].path),
            "--data-dir",
            str(tmp_path / "data"),
            "--out",
            str(out),
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    assert "1 fixes" in result.output
    assert "1 risky" in result.output
    assert out.is_file()
    again = runner.invoke(
        app,
        [
            "mine",
            str(history["repo"].path),
            "--data-dir",
            str(tmp_path / "data"),
            "--out",
            str(out),
        ],
        env={"COLUMNS": "200"},
    )
    assert "0 mined now" in again.output
    fresh = runner.invoke(
        app,
        ["mine", str(history["repo"].path), "--data-dir", str(tmp_path / "data"), "--out",
         str(out), "--fresh"],
        env={"COLUMNS": "200"},
    )  # fmt: skip
    assert "4 mined now" in fresh.output or "5 mined now" in fresh.output


def test_cli_mine_help():
    result = runner.invoke(app, ["mine", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "--data-dir" in result.output and "--fresh" in result.output


def test_missing_train_extra_is_a_friendly_error(history, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pydriller", None)  # makes the import fail
    result = runner.invoke(
        app, ["mine", str(history["repo"].path), "--data-dir", str(tmp_path / "d")],
        env={"COLUMNS": "200"},
    )  # fmt: skip
    assert result.exit_code == 2
    out = " ".join(result.output.split())
    assert "PyDriller is not installed" in out
    assert "pip install 'review-map[train]'" in out
    assert "Traceback" not in out
    monkeypatch.setitem(sys.modules, "pandas", None)
    with pytest.raises(ReviewMapError, match="pandas is not installed"):
        dataset.to_frame([])

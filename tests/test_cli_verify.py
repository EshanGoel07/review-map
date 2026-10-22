"""End-to-end tests for `review-map verify` on a temporary git repo (no LLM, no network)."""

import json

from typer.testing import CliRunner

from review_map.claims import parser
from review_map.cli import app
from review_map.errors import EXIT_USER_ERROR
from tests.test_claims import fake_client

runner = CliRunner()

VIEWS_BEFORE = """\
PAGE_SIZE = 20


def paginate(items, page):
    start = page * PAGE_SIZE
    return items[start:start + PAGE_SIZE - 1]


def can_edit(user, doc):
    if user.role == "admin":
        return True
    return doc.owner_id == user.id
"""

VIEWS_AFTER = """\
PAGE_SIZE = 25


def paginate(items, page):
    start = page * PAGE_SIZE
    return items[start:start + PAGE_SIZE]


def can_edit(user, doc):
    if user.role in ("admin", "editor") or user.is_staff:
        return True
    return doc.owner_id == user.id
"""

CHANGES_MD = """\
# Fix pagination off-by-one

- Fixed the off-by-one in `paginate()` in `app/api/views.py`.
- Added a regression test in `tests/test_views.py`.

## Out of scope
- Payments (`app/payments/`) are not touched.
- The search page.
"""


def _make_feature(git_repo, with_changes_md: bool = True):
    git_repo.write("app/api/views.py", VIEWS_BEFORE)
    git_repo.write("app/payments/refund.py", "def refund(o, a):\n    if a > o.total:\n"
                   "        raise ValueError\n    return o.refund(a)\n")  # fmt: skip
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.write("app/api/views.py", VIEWS_AFTER)
    git_repo.write("app/payments/refund.py", "def refund(o, a):\n    return o.refund(a)\n")
    git_repo.write("poetry.lock", "# lock\n")
    if with_changes_md:
        git_repo.write("changes.md", CHANGES_MD)
    git_repo.commit("feature")


def _verify(git_repo, *args: str):
    return runner.invoke(
        app,
        ["verify", "--repo", str(git_repo.path), "--base", "main", "--no-llm", *args],
        env={"COLUMNS": "250"},  # wide enough that table cells don't wrap
    )


def test_verify_finds_hidden_changes(git_repo):
    _make_feature(git_repo)
    result = _verify(git_repo)
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())  # rich wraps long lines
    assert "Unclaimed changes (2)" in out
    assert "can_edit" in out
    assert "app/api/views.py is mentioned, but this function is not" in out
    assert "Out-of-scope violations (1)" in out
    assert "app/payments/refund.py:refund changed" in out
    assert "Claimed but not changed (1)" in out
    assert "file tests/test_views.py" in out
    assert "The search page." in out  # not checkable
    assert "1 file skipped" in out  # poetry.lock
    assert "changes.md │" not in result.output  # the description isn't a change
    assert "heuristic parser" in out


def test_verify_json(git_repo):
    _make_feature(git_repo)
    result = _verify(git_repo, "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["claims"]["source"] == "heuristic"
    report = data["report"]
    assert {u["name"] for u in report["unclaimed_changes"]} == {"can_edit", "refund"}
    assert report["claimed_but_missing"] == [{"kind": "file", "claim": "tests/test_views.py"}]
    assert [(v["path"], v["name"]) for v in report["out_of_scope_violations"]] == [
        ("app/payments/refund.py", "refund")
    ]
    assert report["skipped_files"] == ["poetry.lock"]
    levels = {c["name"]: c["level"] for c in report["coverage"]}
    assert levels == {
        "<module>": "file",
        "paginate": "function",
        "can_edit": "none",
        "refund": "none",
    }


def test_verify_with_llm_claims(git_repo, monkeypatch):
    _make_feature(git_repo)
    reply = json.dumps(
        {
            "files": ["app/api/views.py", "tests/test_views.py"],
            "functions": ["paginate"],
            "behaviors": ["fix off-by-one in pagination"],
            "out_of_scope": ["app/payments/", "search page"],
        }
    )
    client, _ = fake_client(reply)
    monkeypatch.setattr(parser, "build_client", lambda *a, **k: client)
    result = runner.invoke(app, ["verify", "--repo", str(git_repo.path), "--base", "main"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "groq/openai/gpt-oss-120b" in out
    assert "low confidence" not in out
    assert "2 unclaimed" in out
    assert "Out-of-scope violations (1)" in out
    assert "search page" in out  # prose: not checkable


def test_verify_explicit_desc_outside_repo(git_repo, tmp_path):
    _make_feature(git_repo, with_changes_md=False)
    desc = tmp_path / "pr.md"
    desc.write_text("Changed `can_edit` and `paginate` in app/api/views.py; also refund.\n")
    result = _verify(git_repo, "--desc", str(desc), "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["report"]
    assert [u["name"] for u in report["unclaimed_changes"]] == ["refund"]


def test_verify_desc_from_stdin(git_repo):
    _make_feature(git_repo, with_changes_md=False)
    result = runner.invoke(
        app,
        ["verify", "--repo", str(git_repo.path), "--base", "main", "--no-llm", "--desc", "-",
         "--json"],
        input="Touched `app/` only.\n",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["report"]
    assert report["unclaimed_changes"] == []
    assert {c["level"] for c in report["coverage"]} == {"folder"}


def test_verify_without_description_is_a_clear_error(git_repo):
    _make_feature(git_repo, with_changes_md=False)
    result = _verify(git_repo)
    assert result.exit_code == EXIT_USER_ERROR
    assert "No change description" in result.output
    assert "Traceback" not in result.output


def test_verify_empty_description_flags_everything(git_repo, tmp_path):
    _make_feature(git_repo, with_changes_md=False)
    desc = tmp_path / "empty.md"
    desc.write_text("")
    result = _verify(git_repo, "--desc", str(desc), "--json")
    data = json.loads(result.stdout)
    assert len(data["report"]["unclaimed_changes"]) == len(data["report"]["coverage"]) == 4
    assert any("names no files or functions" in n for n in data["report"]["notes"])


def test_verify_help():
    result = runner.invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    assert "no LLM" in result.output
    assert "changes.md" in result.output

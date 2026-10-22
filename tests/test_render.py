"""Tests for the Markdown, JSON and HTML renderers.

Markdown and JSON are compared with golden files in tests/snapshots/. After an
intended format change, regenerate them and review the diff:

    UPDATE_SNAPSHOTS=1 uv run pytest tests/test_render.py
"""

import json
import os
import re
from pathlib import Path

import pytest

from review_map.code.functions import map_file
from review_map.config import Config, ReadingBudget
from review_map.diff.parser import parse_diff
from review_map.models import ClaimedScope, ClaimSource
from review_map.report import render_html as html_module
from review_map.report import render_json as json_module
from review_map.report import render_md as md_module
from review_map.report.plan import build_plan
from review_map.report.render_html import render_html
from review_map.report.render_json import SCHEMA_VERSION, render_json
from review_map.report.render_md import MARKER, md_code, md_text, render_markdown
from review_map.risk.rules import score_units
from review_map.verify.scope import verify_scope

SNAPSHOTS = Path(__file__).parent / "snapshots"

PERMISSIONS_OLD = """\
\"\"\"Who may do what with a note.\"\"\"


def can_view(user, note) -> bool:
    return note.shared or note.owner_id == user.id or user.is_admin


def can_delete(user, note) -> bool:
    if user.is_admin:
        return True
    return note.owner_id == user.id
"""
PERMISSIONS_NEW = PERMISSIONS_OLD.replace("if user.is_admin:", "if user.is_admin or note.shared:")

API_OLD = """\
from notes.store import NoteStore


def list_notes(store: NoteStore, user, query: dict) -> dict:
    notes = store.list_notes(user.id)
    return {"notes": [n.text for n in notes]}
"""
API_NEW = """\
from notes.store import NoteStore


def list_notes(store: NoteStore, user, query: dict) -> dict:
    page = max(int(query.get("page", 1)), 1)
    notes = store.list_notes(user.id, page=page)
    return {"notes": [n.text for n in notes], "page": page}
"""

DIFF = """\
diff --git a/notes/permissions.py b/notes/permissions.py
index 1111111..2222222 100644
--- a/notes/permissions.py
+++ b/notes/permissions.py
@@ -8,4 +8,4 @@ def can_view(user, note) -> bool:
 def can_delete(user, note) -> bool:
-    if user.is_admin:
+    if user.is_admin or note.shared:
         return True
     return note.owner_id == user.id
diff --git a/notes/api.py b/notes/api.py
index 3333333..4444444 100644
--- a/notes/api.py
+++ b/notes/api.py
@@ -4,3 +4,4 @@ from notes.store import NoteStore
 def list_notes(store: NoteStore, user, query: dict) -> dict:
-    notes = store.list_notes(user.id)
-    return {"notes": [n.text for n in notes]}
+    page = max(int(query.get("page", 1)), 1)
+    notes = store.list_notes(user.id, page=page)
+    return {"notes": [n.text for n in notes], "page": page}
diff --git a/poetry.lock b/poetry.lock
index 5555555..6666666 100644
--- a/poetry.lock
+++ b/poetry.lock
@@ -1 +1 @@
-a = 1
+a = 2
diff --git a/billing/old.py b/billing/new.py
similarity index 100%
rename from billing/old.py
rename to billing/new.py
"""

SOURCES = {
    "notes/permissions.py": (PERMISSIONS_OLD, PERMISSIONS_NEW),
    "notes/api.py": (API_OLD, API_NEW),
}


def make_plan(claims: ClaimedScope | None = None, budget: ReadingBudget | None = None):
    files = parse_diff(DIFF, Config())
    units = [u for f in files if f.path in SOURCES for u in map_file(f, *SOURCES[f.path])]
    scored = score_units(units, files, Config())
    scope = verify_scope(claims, units, files) if claims else None
    return build_plan(
        scored,
        files,
        budget or ReadingBudget(),
        base="main",
        head="feature",
        base_sha="a" * 40,
        head_sha="b" * 40,
        claims=claims,
        scope=scope,
        description="changes.md" if claims else None,
    ), files


CLAIMS = ClaimedScope(
    files=["notes/api.py", "tests/test_api.py"],
    functions=["list_notes"],
    behaviors=["paginate the notes list"],
    out_of_scope=["notes/permissions.py", "billing/", "the login page"],
    source=ClaimSource.LLM,
    model="groq/openai/gpt-oss-120b",
)


@pytest.fixture(autouse=True)
def _fixed_version(monkeypatch):
    for module in (md_module, json_module, html_module):
        monkeypatch.setattr(module, "__version__", "0.0.0-test")


def check_snapshot(name: str, actual: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert path.exists(), f"Missing snapshot {path}; run with UPDATE_SNAPSHOTS=1"
    assert actual == path.read_text(encoding="utf-8"), (
        f"{name} changed; if intended, run UPDATE_SNAPSHOTS=1 uv run pytest tests/test_render.py"
    )


# --- The scenario itself (so the snapshots show what we expect) ------------------


def test_scenario_plan():
    plan, _ = make_plan(CLAIMS)
    names = [(i.unit.name, i.rank, i.priority) for i in plan.items]
    assert names[0][:2] == ("can_delete", 1)  # hidden permission change comes first
    assert plan.items[0].out_of_scope_items == ["notes/permissions.py"]
    assert [v.path for v in plan.file_violations] == ["billing/new.py"]
    assert [m.claim for m in plan.scope.claimed_but_missing] == ["tests/test_api.py"]
    assert plan.skipped_files == ["poetry.lock"]


# --- Markdown -------------------------------------------------------------------


def test_markdown_snapshot():
    plan, _ = make_plan(CLAIMS)
    check_snapshot("reading_plan.md", render_markdown(plan))


def test_markdown_snapshot_without_description():
    plan, _ = make_plan()
    check_snapshot("reading_plan_no_description.md", render_markdown(plan))


def test_markdown_has_marker_and_sections():
    md = render_markdown(make_plan(CLAIMS)[0])
    assert md.rstrip().endswith(MARKER)
    assert md.count(MARKER) == 1
    for heading in (
        "#### Read carefully",
        "#### Unmentioned changes",
        "#### Claimed but not changed",
    ):
        assert heading in md
    assert "Safe to skim" in md


def test_markdown_nothing_stands_out():
    plan, _ = make_plan(CLAIMS, ReadingBudget(max_units=1))
    for item in plan.items:  # pretend everything is low priority
        item.section, item.rank = item.section.SKIM, None
    md = render_markdown(plan)
    assert "Nothing stands out" in md


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a | b", r"a \| b"),
        ("[click](http://evil)", r"\[click\](http://evil)"),
        ("![img](x)", r"!\[img\](x)"),
        ("<img src=x onerror=alert(1)>", r"\<img src=x onerror=alert(1)\>"),
        ("ping @octocat", "ping @​octocat"),
        ("**bold** _it_ `code` ~~s~~", r"\*\*bold\*\* \_it\_ \`code\` \~\~s\~\~"),
        ("line1\nline2", "line1 line2"),
        ("back\\slash", "back\\\\slash"),
    ],
)
def test_md_text_escapes_untrusted_text(text, expected):
    assert md_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("auth.py", "`auth.py`"),
        ("a|b", "`a|b`"),
        ("has`tick", "``has`tick``"),
        ("two``ticks", "```two``ticks```"),
        ("`edge", "`` `edge ``"),
        ("two\nlines", "`two lines`"),
    ],
)
def test_md_code(text, expected):
    assert md_code(text) == expected


def test_md_code_in_tables_escapes_pipes():
    assert md_code("a|b", table=True) == r"`a\|b`"


def test_hostile_names_cannot_break_the_comment():
    claims = CLAIMS.model_copy(update={"out_of_scope": ["see [here](http://x) @admin | <b>"]})
    plan, _ = make_plan(claims)
    plan.items[0].unit.name = "evil|`name`<script>"
    md = render_markdown(plan)
    # Code spans show their content literally; check everything outside them.
    outside = re.sub(r"(`+)(.+?)\1", "", md)
    assert "<script>" not in outside and "<b>" not in outside
    assert "](http://x)" not in outside
    assert "@admin" not in md
    for line in md.splitlines():
        if line.startswith("| 1 |"):
            assert line.count(" | ") == 3  # still exactly 4 cells


# --- JSON --------------------------------------------------------------------------


def test_json_snapshot():
    plan, _ = make_plan(CLAIMS)
    check_snapshot("reading_plan.json", render_json(plan))


def test_json_shape():
    data = json.loads(render_json(make_plan(CLAIMS)[0]))
    assert data["schema_version"] == SCHEMA_VERSION == 1
    assert list(data) == [
        "schema_version", "tool", "change", "summary", "claims", "items",
        "file_violations", "claimed_but_missing", "not_checkable", "skipped_files", "notes",
    ]  # fmt: skip
    item = data["items"][0]
    assert item["unit_id"] == "notes/permissions.py::can_delete"
    assert item["section"] == "read_carefully" and item["rank"] == 1
    assert item["unmentioned"] is True
    assert item["scope"]["out_of_scope"] == ["notes/permissions.py"]
    assert data["summary"]["highest_level"] == "high"
    assert data["summary"]["unmentioned"] == 2  # can_delete + the renamed billing file
    assert "added_text" not in json.dumps(data)  # source code isn't dumped


def test_json_without_description():
    data = json.loads(render_json(make_plan()[0]))
    assert data["claims"] is None
    assert all(i["scope"] is None for i in data["items"])
    assert data["claimed_but_missing"] == [] and data["file_violations"] == []
    assert data["skipped_files"] == ["poetry.lock"]


# --- HTML ------------------------------------------------------------------------------


def test_html_contains_plan_and_highlighted_diff():
    plan, files = make_plan(CLAIMS)
    page = render_html(plan, files)
    assert page.startswith("<!doctype html>")
    assert "Review Map: reading plan" in page
    assert 'class="note high" id="u-1"' in page  # reason note above the flagged lines
    assert 'href="#u-1"' in page
    assert "flag-high" in page  # highlighted lines
    assert "out of scope: the description says" in page
    assert "tests/test_api.py" in page  # claimed but not changed
    assert "poetry.lock" in page and "lockfile" in page  # skipped
    assert "or note.shared:" in page  # the diff itself
    # The flagged file comes before the rest.
    assert page.index("notes/permissions.py</code>") < page.index("notes/api.py</code>\n")


def test_html_is_self_contained():
    page = render_html(*make_plan(CLAIMS))
    assert not re.search(r"(src|href)=\"(https?:)?//", page)
    assert "<script" not in page
    assert "<link" not in page
    assert "prefers-color-scheme: dark" in page


def test_html_escapes_untrusted_text():
    claims = CLAIMS.model_copy(update={"out_of_scope": ["<script>alert(1)</script>"]})
    plan, files = make_plan(claims)
    plan.items[0].unit.name = '<img src=x onerror="alert(1)">'
    files[0].hunks[0].lines[1].text = "<script>steal()</script>"
    page = render_html(plan, files)
    assert "<script>" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;steal()&lt;/script&gt;" in page


def test_html_truncates_huge_diffs(monkeypatch):
    monkeypatch.setattr(html_module, "MAX_ROWS_PER_FILE", 3)
    page = render_html(*make_plan(CLAIMS))
    assert "Diff truncated after 3 lines." in page


def test_html_without_description():
    page = render_html(*make_plan())
    assert "no description" in page
    assert "Unmentioned changes" not in page

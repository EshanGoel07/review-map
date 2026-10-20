"""Tests for finding code units and mapping changed lines to them."""

import pytest
from typer.testing import CliRunner

from review_map.cli import app
from review_map.code.functions import MAX_PARSE_BYTES, collect_units, find_units, map_file, unit_at
from review_map.diff import git_source
from review_map.diff.parser import parse_diff
from review_map.models import MODULE_UNIT, UnitKind


def units_by_name(source: str, language: str) -> dict[str, tuple[str, int, int]]:
    return {u.name: (u.kind.value, u.start_line, u.end_line) for u in find_units(source, language)}


# --------------------------------------------------------------------------- Python

PY = """\
import os


@decorator
@other(1)
async def fetch(url):
    def helper():
        return url
    return helper()


class Outer:
    X = 1

    @property
    def name(self):
        return "o"

    class Inner:
        def deep(self):
            pass


handler = lambda e: e
"""


def test_python_units():
    assert units_by_name(PY, "python") == {
        "fetch": ("function", 4, 9),  # starts at the first decorator
        "fetch.helper": ("function", 7, 8),
        "Outer": ("class", 12, 21),
        "Outer.name": ("method", 15, 17),  # includes @property
        "Outer.Inner": ("class", 19, 21),
        "Outer.Inner.deep": ("method", 20, 21),
    }


def test_python_async_function_detected():
    (unit,) = find_units("async def go():\n    await x\n", "python")
    assert (unit.name, unit.kind) == ("go", UnitKind.FUNCTION)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (1, None),  # import: module level
        (4, "fetch"),  # decorator line belongs to the function
        (8, "fetch.helper"),
        (13, "Outer"),  # class attribute
        (16, "Outer.name"),
        (21, "Outer.Inner.deep"),
        (24, None),  # module-level lambda
    ],
)
def test_unit_at_innermost(line, expected):
    unit = unit_at(find_units(PY, "python"), line)
    assert (unit.name if unit else None) == expected


def test_python_syntax_error_still_finds_units():
    broken = "def ok():\n    return 1\n\ndef broken(:\n    pass\n"
    assert "ok" in units_by_name(broken, "python")


# --------------------------------------------------------------------------- JS / TS

JS = """\
export function main() {
  return 1;
}

export const arrow = async (a) => a + 1;

class Store extends Base {
  items = [];
  onClick = (e) => {
    return e;
  };
  static create() {
    return new Store();
  }
}

function* gen() {}

const obj = { meth() { return 1; } };
"""


def test_javascript_units():
    assert units_by_name(JS, "javascript") == {
        "main": ("function", 1, 3),
        "arrow": ("function", 5, 5),
        "Store": ("class", 7, 15),
        "Store.onClick": ("method", 9, 11),
        "Store.create": ("method", 12, 14),
        "gen": ("function", 17, 17),
    }


TS = """\
@Component({})
export class Widget {
  private x = 1;

  @Get()
  async load(): Promise<void> {
    return;
  }
}

interface Props {
  title: string;
}

type Id = string;

abstract class Shape {
  abstract area(): number;
  describe() {
    return "shape";
  }
}

namespace Util {
  export function helper() {}
}

export default function (a: number) {}
"""


def test_typescript_units():
    assert units_by_name(TS, "typescript") == {
        "Widget": ("class", 1, 9),  # includes class decorator and `export`
        "Widget.load": ("method", 5, 8),  # includes method decorator
        "Props": ("type", 11, 13),
        "Id": ("type", 15, 15),
        "Shape": ("class", 17, 22),
        "Shape.describe": ("method", 19, 21),
        "Util": ("namespace", 24, 26),
        "Util.helper": ("function", 25, 25),
        "default": ("function", 28, 28),  # anonymous default export
    }


def test_tsx_units():
    src = "export const Button = ({ label }: Props) => {\n  return <b>{label}</b>;\n};\n"
    assert units_by_name(src, "tsx") == {"Button": ("function", 1, 3)}


def test_unsupported_language_has_no_units():
    assert find_units("fn main() {}", "rust") == []


def test_huge_file_is_not_parsed():
    assert find_units("x = 1\n" * (MAX_PARSE_BYTES // 6 + 1), "python") == []


# --------------------------------------------------------------------------- mapping

OLD = """\
import os


def keep():
    return 1


def remove_me():
    return 2


class Svc:
    def run(self):
        return 3
"""

NEW = """\
import os
import sys


def keep():
    return 10


class Svc:
    def run(self):
        return 30

    def added(self):
        return 4
"""


def _diff(old: str, new: str, path: str = "app.py") -> str:
    import difflib

    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}"
    )
    return f"diff --git a/{path} b/{path}\n" + "".join(lines)


def test_map_file_groups_lines_by_unit():
    (f,) = parse_diff(_diff(OLD, NEW))
    units = {u.name: u for u in map_file(f, OLD, NEW)}
    assert set(units) == {MODULE_UNIT, "keep", "remove_me", "Svc.run", "Svc.added"}

    module = units[MODULE_UNIT]
    assert module.kind is UnitKind.MODULE
    assert module.added_lines == [2]
    assert (module.start_line, module.end_line) == (2, 2)

    keep = units["keep"]
    assert (keep.added, keep.removed) == (1, 1)
    assert (keep.start_line, keep.end_line) == (5, 6)
    assert (keep.old_start_line, keep.old_end_line) == (4, 5)

    # deleted function: only in the old file
    gone = units["remove_me"]
    assert gone.kind is UnitKind.FUNCTION
    assert gone.start_line is None
    assert (gone.old_start_line, gone.old_end_line) == (8, 9)
    assert gone.added == 0 and gone.removed >= 2

    added = units["Svc.added"]
    assert added.kind is UnitKind.METHOD
    assert added.old_start_line is None
    assert added.removed == 0


def test_map_file_order_follows_the_file():
    (f,) = parse_diff(_diff(OLD, NEW))
    names = [u.name for u in map_file(f, OLD, NEW)]
    assert names.index(MODULE_UNIT) < names.index("keep") < names.index("Svc.run")


def test_removed_only_change_keeps_new_range():
    old = "def f():\n    a = 1\n    b = 2\n    return a\n"
    new = "def f():\n    a = 1\n    return a\n"
    (f,) = parse_diff(_diff(old, new))
    (u,) = map_file(f, old, new)
    assert u.name == "f"
    assert (u.start_line, u.end_line) == (1, 3)
    assert u.removed_lines == [3]


def test_decorator_change_maps_to_function():
    old = "@app.route('/a')\ndef view():\n    return 1\n"
    new = "@app.route('/a', methods=['POST'])\ndef view():\n    return 1\n"
    (f,) = parse_diff(_diff(old, new))
    assert [u.name for u in map_file(f, old, new)] == ["view"]


def test_blank_lines_are_not_attributed():
    old = "class A:\n    def f(self):\n        return 1\n"
    new = "class A:\n    def f(self):\n        return 1\n\n    def g(self):\n        return 2\n"
    (f,) = parse_diff(_diff(old, new))
    (u,) = map_file(f, old, new)
    assert u.name == "A.g"
    assert u.added_lines == [5, 6]


def test_punctuation_only_lines_are_not_attributed():
    """git pairs isAdmin's removed `}` with legacyCheck's; that `}` must not count."""
    old = (
        "class A {\n  isAdmin(u) {\n    return u.admin;\n  }\n\n"
        "  legacy(t) {\n    return t;\n  }\n}\n"
    )
    new = "class A {\n  isAdmin(u) {\n    return u.admin || u.staff;\n  }\n}\n"
    (f,) = parse_diff(_diff(old, new, "a.js"))
    units = {u.name: (u.added, u.removed) for u in map_file(f, old, new)}
    assert units == {"A.isAdmin": (1, 1), "A.legacy": (0, 2)}


def test_js_class_default_export():
    assert units_by_name("export default class {\n  m() {}\n}\n", "javascript") == {
        "default": ("class", 1, 3),
        "default.m": ("method", 2, 2),
    }


def test_unparseable_language_uses_module_bucket():
    old = "a: 1\nb: 2\n"
    new = "a: 1\nb: 3\nc: 4\n"
    (f,) = parse_diff(_diff(old, new, "config.yml"))
    (u,) = map_file(f, old, new)
    assert (u.name, u.kind) == (MODULE_UNIT, UnitKind.MODULE)
    assert u.added_lines == [2, 3]
    assert u.removed_lines == [2]
    assert (u.start_line, u.end_line) == (2, 3)


def test_collect_units_reads_correct_versions():
    """Added files read only the new version; deleted files only the old one."""
    diff_text = (
        "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n+def a():\n+    pass\n"
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-def b():\n-    pass\n"
    )
    files = parse_diff(diff_text)
    calls = []

    def reader(side, text):
        def read(path):
            calls.append((side, path))
            return text.get(path)

        return read

    units = collect_units(
        files,
        read_old=reader("old", {"old.py": "def b():\n    pass\n"}),
        read_new=reader("new", {"new.py": "def a():\n    pass\n"}),
    )
    assert sorted(calls) == [("new", "new.py"), ("old", "old.py")]
    assert {(u.path, u.name) for u in units} == {("new.py", "a"), ("old.py", "b")}


# --------------------------------------------------------------------------- end to end


@pytest.fixture
def py_repo(git_repo):
    git_repo.write("svc/users.py", OLD)
    git_repo.write("svc/renamed_old.py", "def r():\n    return 1\n" + "# pad\n" * 10)
    git_repo.write("yarn.lock", "# lock\n")
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.write("svc/users.py", NEW)
    git_repo.git("mv", "svc/renamed_old.py", "svc/renamed_new.py")
    git_repo.write("svc/renamed_new.py", "def r():\n    return 2\n" + "# pad\n" * 10)
    git_repo.write("yarn.lock", "# lock v2\n")
    git_repo.commit("feature")
    return git_repo


def test_end_to_end_with_git(py_repo):
    root = py_repo.path
    files = parse_diff(git_source.get_diff(root, "main", "feature"))
    mb = git_source.merge_base(root, "main", "feature")
    head = git_source.resolve_ref(root, "feature")
    units = collect_units(
        files,
        read_old=lambda p: git_source.read_file(root, mb, p),
        read_new=lambda p: git_source.read_file(root, head, p),
    )
    names = {(u.path, u.name) for u in units}
    assert ("svc/users.py", "remove_me") in names
    assert ("svc/users.py", "Svc.added") in names
    # renamed file: old version read from the old path
    (r,) = [u for u in units if u.path == "svc/renamed_new.py"]
    assert (r.name, r.old_path, r.old_start_line) == ("r", "svc/renamed_old.py", 1)
    # lockfile is generated, so it is not mapped
    assert not any(u.path == "yarn.lock" for u in units)


def test_read_file_missing_returns_none(py_repo):
    mb = git_source.merge_base(py_repo.path, "main", "feature")
    assert git_source.read_file(py_repo.path, mb, "nope.py") is None


def test_cli_units(py_repo):
    result = CliRunner().invoke(
        app, ["units", "--repo", str(py_repo.path), "--base", "main", "--head", "feature"]
    )
    assert result.exit_code == 0, result.output
    assert "Svc.added" in result.output
    assert "remove_me" in result.output
    assert "(deleted)" in result.output
    assert "1 file skipped" in result.output


def test_repeated_parsing_does_not_crash():
    """Regression: tree-sitter 0.26.0 segfaults on the third parse of this file.

    Run in a subprocess so a crash fails this test instead of killing pytest.
    """
    import subprocess
    import sys
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "code" / "large_module.py"
    script = (
        "import sys\n"
        "from review_map.code.functions import find_units\n"
        "src = open(sys.argv[1]).read()\n"
        "counts = {len(find_units(src, 'python')) for _ in range(20)}\n"
        "print(counts.pop() if len(counts) == 1 else 'unstable')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(fixture)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"parser crashed (exit {result.returncode}): {result.stderr}"
    assert result.stdout.strip() == "13"

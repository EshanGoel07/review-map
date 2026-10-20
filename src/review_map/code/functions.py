"""Map changed lines to functions, classes and methods using tree-sitter.

Added lines are looked up in the new version of the file, removed lines in the
old version (the merge base), so deleted functions are still reported by name.
Lines outside any definition, and files in languages we cannot parse, go to a
`<module>` unit.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache

import tree_sitter as ts
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript

from review_map.models import (
    MODULE_UNIT,
    ChangedFile,
    ChangedUnit,
    CodeUnit,
    FileStatus,
    LineKind,
    UnitKind,
)

MAX_PARSE_BYTES = 1_000_000  # larger files fall back to a single <module> unit

_GRAMMARS: dict[str, Callable[[], object]] = {
    "python": tree_sitter_python.language,
    "javascript": tree_sitter_javascript.language,
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
}

SUPPORTED_LANGUAGES = frozenset(_GRAMMARS)

_PY_CLASS = {"class_definition"}
_PY_FUNC = {"function_definition"}

_JS_CLASS = {"class_declaration", "abstract_class_declaration", "class"}
_JS_FUNC = {"function_declaration", "generator_function_declaration"}
_JS_METHOD = {"method_definition"}
_JS_FIELD = {"public_field_definition", "field_definition"}
_JS_FUNC_VALUES = {"arrow_function", "function_expression", "generator_function", "function"}
_JS_TYPE = {"interface_declaration", "type_alias_declaration", "enum_declaration"}
_JS_NAMESPACE = {"internal_module", "module"}


@cache
def _parser(language: str) -> ts.Parser:
    return ts.Parser(ts.Language(_GRAMMARS[language]()))


def _text(node: ts.Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode("utf-8", errors="replace")


def _start_row(node: ts.Node) -> int:
    """Start row including decorators and `export` keywords in front of the definition."""
    start = node
    for wrappers in (("lexical_declaration", "variable_declaration"), ("decorated_definition",)):
        if start.parent is not None and start.parent.type in wrappers:
            start = start.parent
    if start.parent is not None and start.parent.type == "export_statement":
        start = start.parent
    # TS/JS method decorators are siblings that come right before the method.
    prev = start.prev_named_sibling
    while prev is not None and prev.type == "decorator":
        start = prev
        prev = prev.prev_named_sibling
    return min(start.start_point.row, node.start_point.row)


def _default_export_name(node: ts.Node) -> str:
    parent = node.parent
    return "default" if parent is not None and parent.type == "export_statement" else "<anonymous>"


def _classify(node: ts.Node, language: str, in_class: bool) -> tuple[UnitKind, str] | None:
    """Return (kind, name) if `node` defines a unit, else None."""
    t = node.type
    if language == "python":
        if t in _PY_CLASS:
            return UnitKind.CLASS, _text(node.child_by_field_name("name")) or "?"
        if t in _PY_FUNC:
            kind = UnitKind.METHOD if in_class else UnitKind.FUNCTION
            return kind, _text(node.child_by_field_name("name")) or "?"
        return None

    if t in _JS_CLASS:
        name = _text(node.child_by_field_name("name"))
        if name is None and node.parent is not None and node.parent.type == "variable_declarator":
            return None  # `const A = class {}` is named by its declarator
        return UnitKind.CLASS, name or _default_export_name(node)
    if t in _JS_FUNC_VALUES and node.parent is not None and node.parent.type == "export_statement":
        # `export default function () {}` / `export default () => ...`
        return UnitKind.FUNCTION, _text(node.child_by_field_name("name")) or "default"
    if t in _JS_FUNC:
        return UnitKind.FUNCTION, _text(node.child_by_field_name("name")) or "<anonymous>"
    if t in _JS_METHOD and in_class:
        return UnitKind.METHOD, _text(node.child_by_field_name("name")) or "?"
    if t in _JS_FIELD and in_class:
        value = node.child_by_field_name("value")
        if value is not None and value.type in _JS_FUNC_VALUES:
            name = node.child_by_field_name("name") or node.child_by_field_name("property")
            return UnitKind.METHOD, _text(name) or "?"
        return None
    if t == "variable_declarator":
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name_node is None or name_node.type != "identifier" or value is None:
            return None
        if value.type in _JS_FUNC_VALUES:
            return UnitKind.FUNCTION, _text(name_node) or "?"
        if value.type in _JS_CLASS:
            return UnitKind.CLASS, _text(name_node) or "?"
        return None
    if t in _JS_TYPE:
        return UnitKind.TYPE, _text(node.child_by_field_name("name")) or "?"
    if t in _JS_NAMESPACE:
        return UnitKind.NAMESPACE, _text(node.child_by_field_name("name")) or "?"
    return None


def find_units(source: str | bytes, language: str) -> list[CodeUnit]:
    """List every function/class/method in `source`, outermost first."""
    if language not in SUPPORTED_LANGUAGES:
        return []
    data = source.encode("utf-8") if isinstance(source, str) else source
    if len(data) > MAX_PARSE_BYTES:
        return []
    tree = _parser(language).parse(data)

    units: list[CodeUnit] = []

    def walk(node: ts.Node, prefix: str, in_class: bool) -> None:
        for child in node.named_children:
            found = _classify(child, language, in_class)
            if found is None:
                walk(child, prefix, in_class)
                continue
            kind, name = found
            qualified = f"{prefix}.{name}" if prefix else name
            units.append(
                CodeUnit(
                    name=qualified,
                    kind=kind,
                    start_line=_start_row(child) + 1,
                    end_line=child.end_point.row + 1,
                )
            )
            walk(child, qualified, in_class=kind is UnitKind.CLASS)

    walk(tree.root_node, "", in_class=False)
    return units


def unit_at(units: list[CodeUnit], line: int) -> CodeUnit | None:
    """The innermost unit containing `line`, or None for top-level code."""
    best = None
    for unit in units:
        if unit.start_line <= line <= unit.end_line and (
            best is None or unit.start_line >= best.start_line
        ):
            best = unit
    return best


def _has_content(text: str) -> bool:
    """False for blank or punctuation-only lines (`}`, `);`): nothing to review there,
    and git often pairs them with a different unit than the one that really changed."""
    return any(c.isalnum() for c in text)


@dataclass
class _Acc:
    kind: UnitKind
    new: CodeUnit | None = None
    old: CodeUnit | None = None
    added: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    added_text: list[str] = field(default_factory=list)
    removed_text: list[str] = field(default_factory=list)


def map_file(
    file: ChangedFile, old_source: str | None, new_source: str | None
) -> list[ChangedUnit]:
    """Group a file's changed lines into ChangedUnits."""
    language = file.language if file.language in SUPPORTED_LANGUAGES else None
    new_units = find_units(new_source, language) if language and new_source is not None else []
    old_units = find_units(old_source, language) if language and old_source is not None else []

    acc: dict[str, _Acc] = {}

    def bucket(unit: CodeUnit | None) -> _Acc:
        key = unit.name if unit else MODULE_UNIT
        if key not in acc:
            acc[key] = _Acc(kind=unit.kind if unit else UnitKind.MODULE)
        return acc[key]

    for hunk in file.hunks:
        for line in hunk.lines:
            if not _has_content(line.text):
                continue
            if line.kind is LineKind.ADDED and line.new_lineno:
                unit = unit_at(new_units, line.new_lineno)
                b = bucket(unit)
                b.new = b.new or unit
                b.added.append(line.new_lineno)
                b.added_text.append(line.text)
            elif line.kind is LineKind.REMOVED and line.old_lineno:
                unit = unit_at(old_units, line.old_lineno)
                b = bucket(unit)
                b.old = b.old or unit
                b.removed.append(line.old_lineno)
                b.removed_text.append(line.text)

    # A unit only touched by removals may still exist in the new file (and vice versa).
    new_by_name = {u.name: u for u in reversed(new_units)}
    old_by_name = {u.name: u for u in reversed(old_units)}

    result = []
    for name, a in acc.items():
        if name == MODULE_UNIT:
            new_range = (min(a.added), max(a.added)) if a.added else (None, None)
            old_range = (min(a.removed), max(a.removed)) if a.removed else (None, None)
        else:
            new_u = a.new or new_by_name.get(name)
            old_u = a.old or old_by_name.get(name)
            new_range = (new_u.start_line, new_u.end_line) if new_u else (None, None)
            old_range = (old_u.start_line, old_u.end_line) if old_u else (None, None)
        result.append(
            ChangedUnit(
                path=file.path,
                old_path=file.old_path,
                language=file.language,
                name=name,
                kind=a.kind,
                start_line=new_range[0],
                end_line=new_range[1],
                old_start_line=old_range[0],
                old_end_line=old_range[1],
                added_lines=a.added,
                removed_lines=a.removed,
                added_text=a.added_text,
                removed_text=a.removed_text,
            )
        )
    result.sort(key=lambda u: (u.start_line or u.old_start_line or 0, u.name))
    return result


SourceReader = Callable[[str], str | None]


def collect_units(
    files: list[ChangedFile], read_old: SourceReader, read_new: SourceReader
) -> list[ChangedUnit]:
    """Map every reviewable file. `read_old(path)` / `read_new(path)` return file text or None."""
    units = []
    for f in files:
        if not f.is_reviewable or not f.hunks:
            continue
        old_source = new_source = None
        if f.language in SUPPORTED_LANGUAGES:
            if f.status is not FileStatus.ADDED:
                old_source = read_old(f.old_path or f.path)
            if f.status is not FileStatus.DELETED:
                new_source = read_new(f.path)
        units.extend(map_file(f, old_source, new_source))
    return units

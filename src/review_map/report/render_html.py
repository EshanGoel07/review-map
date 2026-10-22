"""Render a self-contained HTML report: the plan, then each file's diff with the
flagged sections highlighted and their reasons shown next to them.

One file, no JavaScript, no external CSS, fonts or images, so it works offline
and can be attached to a CI run. Everything from the PR is untrusted, so Jinja2
autoescaping is on: a function named `<script>` is shown, never run.
"""

from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from review_map import __version__
from review_map.models import ChangedFile, LineKind, PlanItem, ReadingPlan
from review_map.report.plan import all_reasons, line_range

MAX_ROWS_PER_FILE = 3000

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html", "j2"], default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass
class Row:
    kind: str  # "hunk", "added", "removed", "context", "note", "truncated"
    text: str = ""
    old: int | None = None
    new: int | None = None
    level: str | None = None  # highlight: set when the line belongs to a flagged unit
    item: PlanItem | None = None  # for "note" rows


@dataclass
class FileView:
    path: str
    old_path: str | None
    status: str
    items: list[PlanItem]
    rows: list[Row] = field(default_factory=list)
    flagged: bool = False


def _is_flagged(item: PlanItem) -> bool:
    return item.rank is not None or item.is_unmentioned


def _file_view(f: ChangedFile, items: list[PlanItem]) -> FileView:
    flagged = [i for i in items if _is_flagged(i)]
    owner: dict[tuple[str, int], PlanItem] = {}
    for item in flagged:
        owner.update({("new", n): item for n in item.unit.added_lines})
        owner.update({("old", n): item for n in item.unit.removed_lines})

    view = FileView(f.path, f.old_path, f.status.value, items, flagged=bool(flagged))
    noted: set[str] = set()
    for hunk in f.hunks:
        view.rows.append(
            Row("hunk", f"@@ -{hunk.old_start},{hunk.old_length} "
                        f"+{hunk.new_start},{hunk.new_length} @@ {hunk.section_header}".rstrip())
        )  # fmt: skip
        for line in hunk.lines:
            if line.kind is LineKind.ADDED:
                item = owner.get(("new", line.new_lineno or 0))
            elif line.kind is LineKind.REMOVED:
                item = owner.get(("old", line.old_lineno or 0))
            else:
                item = None
            if item is not None and item.unit_id not in noted:
                noted.add(item.unit_id)
                view.rows.append(Row("note", item=item, level=item.level.value))
            view.rows.append(
                Row(
                    line.kind.value,
                    line.text,
                    old=line.old_lineno,
                    new=line.new_lineno,
                    level=item.level.value if item else None,
                )
            )
        if len(view.rows) > MAX_ROWS_PER_FILE:
            view.rows = view.rows[:MAX_ROWS_PER_FILE]
            view.rows.append(Row("truncated", f"Diff truncated after {MAX_ROWS_PER_FILE} lines."))
            break
    return view


def render_html(plan: ReadingPlan, files: list[ChangedFile]) -> str:
    by_path: dict[str, list[PlanItem]] = {}
    for item in plan.items:
        by_path.setdefault(item.unit.path, []).append(item)

    views = [_file_view(f, by_path.get(f.path, [])) for f in files if f.is_reviewable and f.hunks]

    def order(v: FileView) -> tuple[int, int]:
        ranks = [i.rank for i in v.items if i.rank is not None]
        if ranks:
            return (0, min(ranks))
        return (1, 0) if v.flagged else (2, 0)

    views.sort(key=order)  # stable: diff order within each group
    template = _env.get_template("report.html.j2")
    return template.render(
        plan=plan,
        files=views,
        skipped=[f for f in files if not f.is_reviewable],
        version=__version__,
        reasons=all_reasons,
        line_range=line_range,
    )

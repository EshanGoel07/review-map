"""Render the reading plan as Markdown (GitHub-flavoured; also the PR comment in Phase 8).

Paths, names and claim text come from the PR, so they're untrusted: everything is
escaped so it can't break tables, add links or images, or @-mention people.
The footer marker lets the GitHub Action find and update its own comment.
"""

import re

from review_map import __version__
from review_map.models import PlanItem, ReadingPlan
from review_map.report.plan import all_reasons, line_range

MARKER = "<!-- review-map -->"
MAX_SKIM_LISTED = 50

# Enough to stop emphasis, code, links/images (no "[") and raw HTML (no "<").
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~])")


def md_text(text: str) -> str:
    """Escape text so Markdown/HTML in it shows literally; no @-mentions."""
    text = " ".join(text.split())  # no newlines: they'd end table rows and lists
    text = _MD_SPECIAL.sub(r"\\\1", text)
    return text.replace("@", "@​")  # zero-width space: "@name" won't ping anyone


def md_code(text: str, table: bool = False) -> str:
    """Inline code: shown literally. The fence is longer than any backtick run inside,
    so the text can't close it; in table cells, pipes are escaped too."""
    text = " ".join(text.split())
    if table:
        text = text.replace("|", "\\|")
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _where(item: PlanItem, table: bool = False) -> str:
    path, name = md_code(item.unit.path, table), md_code(item.unit.name, table)
    return f"{path} {name} (L{line_range(item)})"


def _claims_label(plan: ReadingPlan) -> str:
    if plan.claims is None:
        return "no description"
    source = plan.claims.model or "heuristic parser, low confidence"
    return f"claims from {md_code(plan.description or 'description')} ({md_text(source)})"


def render_markdown(plan: ReadingPlan) -> str:
    s = plan.summary
    read = plan.read_carefully
    out: list[str] = []

    out.append("### Review Map: reading plan")
    out.append("")
    pct = f", {round(100 * s.read_lines / s.lines_changed)}%" if s.lines_changed else ""
    out.append(
        f"**Read {len(read)} of {_plural(s.units_changed, 'changed section')} carefully** "
        f"({s.read_lines} of {_plural(s.lines_changed, 'changed line')}{pct}) · "
        f"{len(plan.skim)} to skim · {_plural(s.files_skipped, 'file')} skipped · "
        f"{_claims_label(plan)}"
    )

    unmentioned = plan.unmentioned
    violations = [i for i in unmentioned if i.out_of_scope_items] + plan.file_violations
    if unmentioned or plan.file_violations:
        parts = []
        if violations:
            parts.append(f"{len(violations)} changed although the description says out of scope")
        unclaimed = sum(not i.out_of_scope_items for i in unmentioned)
        if unclaimed:
            parts.append(f"{unclaimed} not mentioned in the description")
        out += ["", f"> **Heads up:** {'; '.join(parts)}."]

    out += ["", "#### Read carefully", ""]
    if read:
        out.append("| # | Where | Priority | Why |")
        out.append("|---|---|---|---|")
        for item in read:
            why = " · ".join(md_text(r) for r in all_reasons(item)) or "-"
            out.append(
                f"| {item.rank} | {_where(item, table=True)} | "
                f"{item.priority} {item.level.value} | {why} |"
            )
    else:
        out.append("_Nothing stands out. Skim everything._")

    if unmentioned or plan.file_violations:
        out += ["", "#### Unmentioned changes", ""]
        for item in unmentioned:
            out.append(f"- {_where(item)}: {md_text('; '.join(item.scope_reasons))}")
        for v in plan.file_violations:
            out.append(
                f"- {md_code(v.path)}: " + md_text(f'out of scope: the description says "{v.item}"')
            )

    if plan.scope and plan.scope.claimed_but_missing:
        out += ["", "#### Claimed but not changed", ""]
        for m in plan.scope.claimed_but_missing:
            out.append(f"- {m.kind} {md_code(m.claim)}")

    skim = plan.skim
    if skim:
        out += [
            "",
            f"<details><summary>Safe to skim: {_plural(len(skim), 'section')}</summary>",
            "",
        ]
        for item in skim[:MAX_SKIM_LISTED]:
            out.append(f"- {_where(item)}: {item.priority} {item.level.value}")
        if len(skim) > MAX_SKIM_LISTED:
            out.append(f"- … and {len(skim) - MAX_SKIM_LISTED} more")
        out += ["", "</details>"]

    if plan.notes:
        out += ["", *[f"> {md_text(n)}" for n in plan.notes]]

    out += [
        "",
        "---",
        f"<sub>Review Map {__version__} · a reading plan, not a review. "
        "The description is treated as untrusted input and checked against the diff.</sub>",
        "",
        MARKER,
        "",
    ]
    return "\n".join(out)

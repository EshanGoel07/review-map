"""Command-line interface for Review Map."""

import io
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from review_map import __version__
from review_map.claims.llm import in_github_actions
from review_map.claims.parser import parse_claims
from review_map.config import load_config
from review_map.diff import git_source
from review_map.diff.github_pr import (
    PullRequest,
    default_repo_cache_dir,
    ensure_pr_commits,
    fetch_pr,
    git_auth,
    load_pr_config,
    parse_pr_ref,
)
from review_map.diff.parser import parse_diff
from review_map.errors import EXIT_GATE_FAILED, ReviewMapError
from review_map.github import action
from review_map.github.client import GitHubClient, token_from_env
from review_map.models import (
    ChangedFile,
    ChangedUnit,
    ClaimedScope,
    FileStatus,
    MatchLevel,
    PlanItem,
    ReadingPlan,
    RiskLevel,
    ScopeReport,
    UnitKind,
)
from review_map.pipeline import (
    PR_DESCRIPTION,
    PR_NO_DESCRIPTION_NOTE,
    LocalChange,
    PlanRun,
    collect,
    plan_for,
    pr_change,
)
from review_map.report.plan import (
    all_reasons,
    gate_failed,
    line_range,
)
from review_map.report.render_html import render_html
from review_map.report.render_json import render_json
from review_map.report.render_md import render_markdown
from review_map.risk.rules import score_units
from review_map.verify.scope import verify_scope

app = typer.Typer(
    name="review-map",
    help=(
        "Review Map: get a short reading plan for a pull request.\n\n"
        "It tells you which few changes to read carefully, in which order, "
        "and which changes the description did not mention."
    ),
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

RepoOption = Annotated[
    Path, typer.Option("--repo", "-r", help="Path to the git repository.", show_default=True)
]
BaseOption = Annotated[
    str | None,
    typer.Option(help="Base ref (branch, tag or SHA). Default: origin's default, main or master."),
]
HeadOption = Annotated[str, typer.Option(help="Head ref with the changes to review.")]
ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="Config file. Default: .reviewmap.yml in the repo, if any."),
]


@contextmanager
def friendly_errors() -> Iterator[None]:
    """Print expected errors as a short message instead of a traceback."""
    try:
        yield
    except ReviewMapError as exc:
        err_console.print(f"[bold red]Error:[/] {escape(exc.message)}")
        if exc.hint:
            err_console.print(f"[dim]Hint:[/] {escape(exc.hint)}")
        if in_github_actions():  # also show it on the run's summary page
            hint = f" {exc.hint}" if exc.hint else ""
            action.annotate("error", exc.message + hint, sys.stderr)
        raise typer.Exit(exc.exit_code) from None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def load_env_file() -> None:
    """Load `.env` from the current directory (or a parent). Real env vars win."""
    load_dotenv(find_dotenv(usecwd=True), override=False)


@app.callback()
def main() -> None:
    """Review Map: get a short reading plan for a pull request."""
    # Inside GitHub Actions the working tree is the PR's own code: a `.env` there could
    # point the LLM at another server and receive the repo's API key. Settings come
    # only from the workflow there.
    if not in_github_actions():
        load_env_file()


@app.command()
def version() -> None:
    """Show the installed Review Map version."""
    console.print(f"review-map {__version__}")


def _load_local(repo: Path, base: str | None, head: str, config: Path | None) -> LocalChange:
    root = git_source.find_repo_root(repo)
    loaded = load_config(root, config)
    base = base or git_source.default_base(root)
    files = parse_diff(git_source.get_diff(root, base, head), loaded.config)
    return LocalChange(
        root=root,
        base=base,
        head=head,
        merge_base=git_source.merge_base(root, base, head),
        head_sha=git_source.resolve_ref(root, head),
        loaded=loaded,
        files=files,
    )


def _print_header(title: str, change: LocalChange) -> None:
    loaded = change.loaded
    source = str(loaded.source.relative_to(change.root)) if loaded.source else "defaults"
    console.print(
        f"[bold]{title}[/] {escape(change.base)}...{escape(change.head)} in "
        f"{escape(str(change.root))}  [dim](config: {escape(source)})[/]"
    )


_STATUS_STYLE = {
    FileStatus.ADDED: "[green]added[/]",
    FileStatus.MODIFIED: "[yellow]modified[/]",
    FileStatus.DELETED: "[red]deleted[/]",
    FileStatus.RENAMED: "[cyan]renamed[/]",
}


def _file_note(f: ChangedFile) -> str:
    if f.is_binary:
        return "binary, skipped"
    if f.generated_reason:
        return f"{f.generated_reason}, skimmed"
    if f.ignored_by:
        return f"ignored ({f.ignored_by})"
    if not f.hunks:
        return "no content change"
    return ""


@app.command()
def diff(
    repo: RepoOption = Path("."),
    base: BaseOption = None,
    head: HeadOption = "HEAD",
    config: ConfigOption = None,
) -> None:
    """List the files changed between BASE and HEAD (what a PR from HEAD into BASE shows)."""
    with friendly_errors():
        change = _load_local(repo, base, head, config)
    _print_header("Diff", change)
    files = change.files
    if not files:
        console.print("No changes.")
        return

    table = Table(show_edge=False, header_style="bold")
    table.add_column("Status")
    table.add_column("File", overflow="fold")
    table.add_column("Lang")
    table.add_column("+", justify="right", style="green")
    table.add_column("-", justify="right", style="red")
    table.add_column("Hunks", justify="right")
    table.add_column("Note", style="dim")
    for f in files:
        name = f"{f.old_path} → {f.path}" if f.old_path else f.path
        table.add_row(
            _STATUS_STYLE[f.status],
            escape(name),
            f.language or "-",
            str(f.added),
            str(f.removed),
            str(len(f.hunks)),
            escape(_file_note(f)),
        )
    console.print(table)

    reviewable = sum(f.is_reviewable for f in files)
    console.print(
        f"{_plural(len(files), 'file')} changed, [green]+{sum(f.added for f in files)}[/] "
        f"[red]-{sum(f.removed for f in files)}[/] · {reviewable} to review, "
        f"{len(files) - reviewable} skipped (binary, generated or ignored)"
    )


def _line_range(u: ChangedUnit) -> str:
    if u.start_line is not None:
        return f"{u.start_line}-{u.end_line}"
    return f"old {u.old_start_line}-{u.old_end_line} (deleted)"


@app.command()
def units(
    repo: RepoOption = Path("."),
    base: BaseOption = None,
    head: HeadOption = "HEAD",
    config: ConfigOption = None,
) -> None:
    """List the functions, classes and methods touched between BASE and HEAD."""
    with friendly_errors():
        change = _load_local(repo, base, head, config)
        changed = collect(change)
    _print_header("Units", change)
    if not changed:
        console.print("No code changes to map.")
        return

    table = Table(show_edge=False, header_style="bold")
    table.add_column("File", overflow="fold")
    table.add_column("Unit", overflow="fold")
    table.add_column("Kind")
    table.add_column("Lines", justify="right")
    table.add_column("+", justify="right", style="green")
    table.add_column("-", justify="right", style="red")
    previous = None
    for u in changed:
        table.add_row(
            escape(u.path) if u.path != previous else "",
            escape(u.name),
            u.kind.value,
            _line_range(u),
            str(u.added),
            str(u.removed),
        )
        previous = u.path
    console.print(table)
    n_files = len({u.path for u in changed})
    skipped = sum(not f.is_reviewable for f in change.files)
    console.print(
        f"{_plural(len(changed), 'unit')} changed in {_plural(n_files, 'file')}"
        + (
            f" · {_plural(skipped, 'file')} skipped (binary, generated or ignored)"
            if skipped
            else ""
        )
    )


_LEVEL_STYLE = {RiskLevel.HIGH: "bold red", RiskLevel.MEDIUM: "yellow", RiskLevel.LOW: "dim"}


@app.command()
def score(
    repo: RepoOption = Path("."),
    base: BaseOption = None,
    head: HeadOption = "HEAD",
    config: ConfigOption = None,
    limit: Annotated[int, typer.Option(help="Show at most N units (0 = all).")] = 20,
) -> None:
    """Rank changed units by rule-based risk score (0-100), with reasons."""
    with friendly_errors():
        change = _load_local(repo, base, head, config)
        scored = score_units(collect(change), change.files, change.loaded.config)
    _print_header("Risk", change)
    if not scored:
        console.print("No code changes to score.")
        return

    shown = scored if limit <= 0 else scored[:limit]
    table = Table(show_edge=False, header_style="bold", show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Score", justify="right")
    table.add_column("Unit", overflow="fold", ratio=2)
    table.add_column("Reasons", overflow="fold", ratio=3)
    for i, s in enumerate(shown, 1):
        style = _LEVEL_STYLE[s.risk.level]
        reasons = "; ".join(r.message for r in s.risk.reasons if r.points > 0) or "-"
        table.add_row(
            str(i),
            f"[{style}]{s.risk.score} {s.risk.level.value}[/]",
            f"{escape(s.unit.path)}:{_line_range(s.unit)}\n[bold]{escape(s.unit.name)}[/]",
            escape(reasons),
        )
    console.print(table)

    counts = {level: sum(s.risk.level is level for s in scored) for level in RiskLevel}
    more = f" · {len(scored) - len(shown)} more (use --limit 0)" if len(shown) < len(scored) else ""
    console.print(
        f"{_plural(len(scored), 'unit')}: [bold red]{counts[RiskLevel.HIGH]} high[/], "
        f"[yellow]{counts[RiskLevel.MEDIUM]} medium[/], {counts[RiskLevel.LOW]} low{more}"
    )


def _read_description(desc: Path) -> str:
    if str(desc) == "-":
        return typer.get_text_stream("stdin").read()
    try:
        return desc.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ReviewMapError(f"Description file not found: {desc}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ReviewMapError(f"Could not read {desc}: {exc}") from None


def _print_claims(scope: ClaimedScope, desc: Path, cached: bool) -> None:
    parser = scope.model if scope.model else "heuristic parser"
    extra = ", cached" if cached else ""
    confidence = " · [yellow]low confidence[/]" if scope.low_confidence else ""
    console.print(
        f"[bold]Claims[/] from {escape(str(desc))}  [dim]({escape(parser)}{extra})[/]{confidence}"
    )
    sections = [
        ("Files", scope.files),
        ("Functions", scope.functions),
        ("Behaviors", scope.behaviors),
        ("Out of scope", scope.out_of_scope),
    ]
    for title, items in sections:
        console.print(f"\n[bold]{title}[/] ({len(items)})")
        for item in items:
            console.print(f"  • {escape(item)}")
        if not items:
            console.print("  [dim](none)[/]")
    for note in scope.notes:
        err_console.print(f"\n[yellow]Note:[/] {escape(note)}")


NoLLMOption = Annotated[
    bool, typer.Option("--no-llm", help="Skip the LLM; use the heuristic parser.")
]
NoCacheOption = Annotated[
    bool, typer.Option("--no-cache", help="Ask the LLM again instead of using the cache.")
]


@app.command()
def claims(
    desc: Annotated[
        Path,
        typer.Option(
            "--desc", "-d", help="Change description (e.g. changes.md), or - to read stdin."
        ),
    ],
    config: ConfigOption = None,
    no_llm: NoLLMOption = False,
    no_cache: NoCacheOption = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the claims as JSON.")] = False,
) -> None:
    """Turn a change description into a structured claimed scope (files, functions...).

    The description is treated as untrusted data. The LLM is set in .env (see
    .env.example); without one, a heuristic parser is used.
    """
    with friendly_errors():
        text = _read_description(desc)
        loaded = load_config(Path.cwd(), config)
        scope, cached = parse_claims(
            text, loaded.config.llm, use_llm=not no_llm, use_cache=not no_cache
        )
    if as_json:
        typer.echo(scope.model_dump_json(indent=2))
    else:
        _print_claims(scope, desc, cached)


DEFAULT_DESC = "changes.md"


def _resolve_desc(desc: Path | None, root: Path) -> Path:
    """`--desc` if given, else `changes.md` in the repo root."""
    if desc is not None:
        return desc
    default = root / DEFAULT_DESC
    if default.is_file():
        return default
    raise ReviewMapError(
        f"No change description: pass --desc FILE (no {DEFAULT_DESC} in {root}).",
        hint="Write what the change claims to do in changes.md, or pass the PR text with --desc.",
    )


def _repo_relative(path: Path, root: Path) -> str | None:
    """`path` relative to the repo root, or None if it's stdin or outside the repo."""
    if str(path) == "-":
        return None
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _print_scope(report: ScopeReport, units_by_id: dict[str, ChangedUnit]) -> None:
    counts = {level: sum(c.level is level for c in report.coverage) for level in MatchLevel}
    console.print(
        f"{_plural(len(report.coverage), 'changed unit')}: "
        f"{counts[MatchLevel.FUNCTION]} claimed by name, {counts[MatchLevel.FILE]} by file, "
        f"{counts[MatchLevel.FOLDER]} by folder, "
        f"[bold red]{counts[MatchLevel.NONE]} unclaimed[/]"
    )

    unclaimed = report.unclaimed_changes
    console.print(f"\n[bold]Unclaimed changes[/] ({len(unclaimed)})")
    if unclaimed:
        table = Table(show_edge=False, header_style="bold")
        table.add_column("File", overflow="fold")
        table.add_column("Unit", overflow="fold")
        table.add_column("Lines", justify="right")
        table.add_column("Note", style="dim", overflow="fold")
        for c in unclaimed:
            table.add_row(
                escape(c.path),
                escape(c.name),
                _line_range(units_by_id[c.unit_id]),
                escape(c.note or "not mentioned"),
            )
        console.print(table)
    else:
        console.print("  [dim](none)[/]")

    violations = report.out_of_scope_violations
    console.print(f"\n[bold]Out-of-scope violations[/] ({len(violations)})")
    for v in violations:
        where = f"{v.path}:{v.name}" if v.name else v.path
        console.print(
            f"  • [red]{escape(where)}[/] changed, but the description says it is out of "
            f'scope: "{escape(v.item)}"'
        )
    if not violations:
        console.print("  [dim](none)[/]")

    missing = report.claimed_but_missing
    console.print(f"\n[bold]Claimed but not changed[/] ({len(missing)})")
    for m in missing:
        console.print(f"  • {m.kind} {escape(m.claim)}")
    if not missing:
        console.print("  [dim](none)[/]")

    if report.not_checkable:
        console.print(
            f"\n[dim]Out-of-scope items with no file or name to check: "
            f"{escape('; '.join(report.not_checkable))}[/]"
        )
    if report.skipped_files:
        console.print(
            f"[dim]{_plural(len(report.skipped_files), 'file')} skipped "
            "(binary, generated or ignored)[/]"
        )


@app.command()
def verify(
    repo: RepoOption = Path("."),
    base: BaseOption = None,
    head: HeadOption = "HEAD",
    desc: Annotated[
        Path | None,
        typer.Option(
            "--desc",
            "-d",
            help="Change description, or - for stdin. Default: changes.md in the repo root.",
        ),
    ] = None,
    config: ConfigOption = None,
    no_llm: NoLLMOption = False,
    no_cache: NoCacheOption = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the claims and the report as JSON.")
    ] = False,
) -> None:
    """Check the description against the diff: unclaimed, missing and out-of-scope changes.

    The comparison is plain code (no LLM); only reading the description may use one.
    """
    with friendly_errors():
        change = _load_local(repo, base, head, config)
        changed = collect(change)
        desc = _resolve_desc(desc, change.root)
        scope, cached = parse_claims(
            _read_description(desc),
            change.loaded.config.llm,
            use_llm=not no_llm,
            use_cache=not no_cache,
        )
        report = verify_scope(scope, changed, change.files, _repo_relative(desc, change.root))
    if as_json:
        typer.echo(
            json.dumps(
                {"claims": scope.model_dump(mode="json"), "report": report.model_dump(mode="json")},
                indent=2,
            )
        )
        return

    _print_header("Verify", change)
    parser = scope.model or "heuristic parser"
    confidence = " · [yellow]low confidence[/]" if scope.low_confidence else ""
    console.print(
        f"[dim]Claims from {escape(str(desc))} ({escape(parser)}"
        f"{', cached' if cached else ''})[/]{confidence}"
    )
    _print_scope(report, {u.unit_id: u for u in changed})
    for note in [*scope.notes, *report.notes]:
        err_console.print(f"\n[yellow]Note:[/] {escape(note)}")


class OutputFormat(StrEnum):
    TEXT = "text"
    MD = "md"
    JSON = "json"
    HTML = "html"


class FailOn(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"


DEFAULT_HTML_OUT = Path("report.html")


def _skim_label(item: PlanItem) -> str:
    """A unit's name, or its file for module-level code (a bare `<module>` says nothing)."""
    if item.unit.kind is UnitKind.MODULE:
        return item.unit.path
    return item.unit.name


def _print_plan(plan: ReadingPlan, con: Console) -> None:
    s = plan.summary
    read = plan.read_carefully
    con.print(
        f"\n[bold]Read carefully[/] ({len(read)} of {_plural(s.units_changed, 'section')}, "
        f"{s.read_lines} of {_plural(s.lines_changed, 'changed line')}; "
        f"budget {s.budget_lines} lines, {s.max_units} sections)"
    )
    if read:
        table = Table(show_edge=False, header_style="bold")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Priority", justify="right")
        table.add_column("Unit", overflow="fold", ratio=2)
        table.add_column("Why", overflow="fold", ratio=3)
        for item in read:
            style = _LEVEL_STYLE[item.level]
            why = [f"[bold red]{escape(r)}[/]" for r in item.scope_reasons]
            why += [escape(r) for r in all_reasons(item)[len(item.scope_reasons) :]]
            table.add_row(
                str(item.rank),
                f"[{style}]{item.priority} {item.level.value}[/]",
                f"{escape(item.unit.path)}:{line_range(item)}\n[bold]{escape(item.unit.name)}[/]",
                "; ".join(why) or "-",
            )
        con.print(table)
    else:
        con.print("  [dim]Nothing stands out. Skim everything.[/]")

    unmentioned = plan.unmentioned
    if unmentioned or plan.file_violations:
        con.print(
            f"\n[bold]Unmentioned changes[/] ({len(unmentioned) + len(plan.file_violations)})"
        )
        for item in unmentioned:
            con.print(
                f"  • [red]{escape(item.unit.path)}:{escape(item.unit.name)}[/] "
                f"{escape('; '.join(item.scope_reasons))}"
            )
        for v in plan.file_violations:
            con.print(f'  • [red]{escape(v.path)}[/] out of scope: "{escape(v.item)}"')

    if plan.scope and plan.scope.claimed_but_missing:
        con.print(f"\n[bold]Claimed but not changed[/] ({len(plan.scope.claimed_but_missing)})")
        for m in plan.scope.claimed_but_missing:
            con.print(f"  • {m.kind} {escape(m.claim)}")

    skim = plan.skim
    if skim:
        names = ", ".join(escape(_skim_label(i)) for i in skim[:8])
        more = f" and {len(skim) - 8} more" if len(skim) > 8 else ""
        con.print(
            f"\n[bold]Safe to skim[/]: {_plural(len(skim), 'section')} [dim]({names}{more})[/]"
        )
    if s.files_skipped:
        con.print(
            f"[dim]{_plural(s.files_skipped, 'file')} skipped (binary, generated or ignored)[/]"
        )


def _write(path: Path, content: str) -> None:
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ReviewMapError(
            f"Could not write {path}: {exc.strerror or exc}",
            hint="Check that the folder exists and is writable.",
        ) from None


def _emit(
    run: PlanRun,
    change: LocalChange,
    output_format: OutputFormat,
    out: Path | None,
    header: Callable[[], None],
) -> None:
    """Write the plan in the chosen format. Text gets `header` and the claims line first."""
    plan = run.plan
    if output_format is OutputFormat.TEXT:
        con = Console(file=io.StringIO(), record=True, width=120) if out else console
        header()  # header always goes to the terminal
        if run.claims is not None:
            source = run.claims.model or "heuristic parser"
            confidence = " · [yellow]low confidence[/]" if run.claims.low_confidence else ""
            console.print(
                f"[dim]Claims from {escape(plan.description or '')} ({escape(source)}"
                f"{', cached' if run.cached else ''})[/]{confidence}"
            )
        _print_plan(plan, con)
        if out:
            _write(out, con.export_text())
            console.print(f"Wrote {escape(str(out))}")
        return
    rendered = {
        OutputFormat.MD: lambda: render_markdown(plan),
        OutputFormat.JSON: lambda: render_json(plan),
        OutputFormat.HTML: lambda: render_html(plan, change.files),
    }[output_format]()
    target = out or (DEFAULT_HTML_OUT if output_format is OutputFormat.HTML else None)
    if target is None:
        typer.echo(rendered, nl=False)
    else:
        _write(target, rendered)
        err_console.print(f"Wrote {escape(str(target))}")


def _finish(plan: ReadingPlan, output_format: OutputFormat, fail_on: FailOn | None) -> None:
    """Print notes (text format) and apply the `--fail-on` gate."""
    if output_format is OutputFormat.TEXT:
        for note in plan.notes:
            err_console.print(f"\n[yellow]Note:[/] {escape(note)}")

    failing = gate_failed(plan, fail_on.value if fail_on else None)
    if failing:
        err_console.print(
            f"[bold red]Gate failed[/] (--fail-on {fail_on.value}): "
            f"{_plural(len(failing), 'section')} at {fail_on.value} priority or above."
        )
        raise typer.Exit(EXIT_GATE_FAILED)


FormatOption = Annotated[OutputFormat, typer.Option("--format", "-f", help="Output format.")]
OutOption = Annotated[
    Path | None,
    typer.Option(
        "--out", "-o", help="Write to this file. Default: the terminal (html: report.html)."
    ),
]
FailOnOption = Annotated[
    FailOn | None,
    typer.Option(
        "--fail-on",
        help="Exit with code 1 if any change reaches this priority (for CI gates).",
    ),
]


@app.command()
def analyze(
    repo: RepoOption = Path("."),
    base: BaseOption = None,
    head: HeadOption = "HEAD",
    desc: Annotated[
        Path | None,
        typer.Option(
            "--desc",
            "-d",
            help="Change description, or - for stdin. Default: changes.md in the repo root, "
            "if any; without one the scope check is skipped.",
        ),
    ] = None,
    output_format: FormatOption = OutputFormat.TEXT,
    out: OutOption = None,
    fail_on: FailOnOption = None,
    config: ConfigOption = None,
    no_llm: NoLLMOption = False,
    no_cache: NoCacheOption = False,
) -> None:
    """Build the reading plan: what to read carefully, in which order, and what to skim.

    Combines the risk score of every changed function with a check of the description
    against the diff. Unmentioned changes are boosted.
    """
    with friendly_errors():
        change = _load_local(repo, base, head, config)
        text = label = rel = None
        if desc is not None or (change.root / DEFAULT_DESC).is_file():
            desc = _resolve_desc(desc, change.root)
            rel = _repo_relative(desc, change.root)
            label = "stdin" if str(desc) == "-" else (rel or str(desc))
            text = _read_description(desc)
        run = plan_for(change, text, label, rel, no_llm=no_llm, no_cache=no_cache)
        _emit(run, change, output_format, out, lambda: _print_header("Review Map", change))
    _finish(run.plan, output_format, fail_on)


def _print_pr_header(pr: PullRequest, change: LocalChange, fetched: bool) -> None:
    loaded = change.loaded
    source = str(loaded.source) + " from base" if loaded.source else "defaults"
    fork = " · from a fork" if pr.from_fork else ""
    git = "fetched" if fetched else "cached"
    console.print(
        f"[bold]Review Map[/] {escape(pr.ref)} [dim]({escape(pr.status)}{fork})[/]: "
        f"{escape(pr.title)}"
    )
    console.print(
        f"[dim]{escape(change.base)}...{escape(change.head)} · {escape(pr.html_url)} · "
        f"commits {git} · config: {escape(source)}[/]"
    )


@app.command("pr")
def pull_request(
    ref: Annotated[
        str,
        typer.Argument(
            metavar="PR",
            help="owner/repo#123 or https://github.com/owner/repo/pull/123",
            show_default=False,
        ),
    ],
    output_format: FormatOption = OutputFormat.TEXT,
    out: OutOption = None,
    fail_on: FailOnOption = None,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Config file. Default: .reviewmap.yml from the PR's base commit, if any.",
        ),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option(
            "--cache-dir",
            help="Where repo clones are kept. Default: $REVIEW_MAP_REPO_CACHE_DIR, "
            "else ~/.cache/review-map/repos.",
            show_default=False,
        ),
    ] = None,
    no_llm: NoLLMOption = False,
    no_cache: NoCacheOption = False,
) -> None:
    """Build the reading plan for a GitHub pull request.

    Reads the PR's title, description and commits from GitHub (GITHUB_TOKEN is
    optional for public repos), keeps a partial clone in a cache folder, and runs
    the same analysis as `analyze`. The PR description is treated as untrusted data.
    """
    with friendly_errors():
        pr_ref = parse_pr_ref(ref)
        token = token_from_env()
        with GitHubClient(token) as client:
            pr = fetch_pr(client, pr_ref)
        with git_auth(token):
            clone, fetched = ensure_pr_commits(pr, cache_dir or default_repo_cache_dir())
            change = pr_change(clone, pr, load_pr_config(clone, pr.base_sha, config))
            run = plan_for(
                change,
                pr.description,
                PR_DESCRIPTION,
                None,
                no_llm=no_llm,
                no_cache=no_cache,
                missing_note=PR_NO_DESCRIPTION_NOTE,
            )
        _emit(run, change, output_format, out, lambda: _print_pr_header(pr, change, fetched))
    _finish(run.plan, output_format, fail_on)


@app.command()
def mine(
    targets: Annotated[
        list[str],
        typer.Argument(
            metavar="REPO...",
            help="Local repo paths, or GitHub owner/name to clone (e.g. psf/requests).",
            show_default=False,
        ),
    ],
    data_dir: Annotated[
        Path, typer.Option("--data-dir", help="Where clones and work files are kept.")
    ] = Path("data"),
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Dataset file. Default: DATA_DIR/dataset.parquet."),
    ] = None,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="Ignore saved progress and mine from scratch.")
    ] = False,
) -> None:
    """Mine git history into a training dataset (needs the `train` extra).

    One row per changed function per commit; label 1 if a later bug fix changed code
    that commit wrote (function-level SZZ). Interrupted runs resume where they stopped.
    """
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TimeRemainingColumn

    from review_map.training import dataset
    from review_map.training import mine as mining

    with friendly_errors():
        all_rows: list[dict] = []
        all_commits: list[dict] = []
        meta: dict[str, dict] = {}
        for target in targets:
            repo = mining.resolve_repo(target, data_dir / "repos")
            name = repo.resolve().name
            work = data_dir / "work" / name
            if fresh:
                (work / dataset.CHECKPOINT).unlink(missing_ok=True)
            with Progress(
                f"[bold]{escape(name)}[/]",
                BarColumn(),
                MofNCompleteColumn(),
                TimeRemainingColumn(),
                console=err_console,
                transient=True,
            ) as bar:
                task = bar.add_task("mining", total=None)
                rows, stats, head = dataset.build_dataset(
                    repo,
                    work,
                    name,
                    on_progress=lambda done, total, task=task: bar.update(
                        task, completed=done, total=total
                    ),
                    commits_out=all_commits,
                )
            positives = sum(r["label"] for r in rows)
            console.print(
                f"[bold]{escape(name)}[/] at {head[:12]}: {stats.commits} commits "
                f"({stats.skipped_huge} huge, {stats.skipped_root} root and "
                f"{stats.skipped_error} unreadable skipped, "
                f"{stats.processed} mined now), {stats.fixes} fixes, "
                f"{_plural(len(rows), 'row')}, {positives} risky "
                f"({positives / len(rows):.1%})"
                if rows
                else f"[bold]{escape(name)}[/]: no rows"
            )
            if stats.blame_errors:
                err_console.print(
                    f"[bold yellow]Warning:[/] git blame failed on {stats.blame_errors} "
                    "file(s); those fixes labelled nothing, so the labels undercount."
                )
            if stats.fixes and not stats.implicated_lines:
                err_console.print(
                    f"[bold yellow]Warning:[/] {stats.fixes} fixes but no blamed lines: "
                    "the labels are probably wrong. Check git blame works in this repo."
                )
            if stats.implicated_lines:
                console.print(
                    f"  [dim]{stats.implicated_lines} blamed lines, "
                    f"{stats.unmatched_lines} outside any dataset row[/]"
                )
            all_rows += rows
            meta[name] = {"source": target, "head": head, "commits": stats.commits}

        frame = dataset.to_frame(all_rows)
        target_path = out or data_dir / "dataset.parquet"
        dataset.save(frame, target_path)
        dataset.save(dataset.commits_frame(all_commits), dataset.commits_path(target_path))
        dataset.save_meta(target_path, meta)

    total = len(frame)
    positives = int(frame["label"].sum()) if total else 0
    recent = int(frame["too_recent"].sum()) if total else 0
    console.print(
        f"\nWrote {escape(str(target_path))}: {_plural(total, 'row')}, "
        f"[bold]{positives} risky[/] ({positives / total:.1%})"
        if total
        else "\nNo rows."
    )
    if total:
        console.print(
            f"[dim]{recent} rows are from the last {dataset.RECENT_DAYS} days (too_recent): "
            "their label is less certain.[/]"
        )


def _print_results(title: str, results: dict[str, dict], chosen: str | None = None) -> None:
    from review_map.training.evaluate import METRIC_HEADERS, _fmt

    table = Table(title=title, show_edge=False, header_style="bold", title_justify="left")
    table.add_column("Method")
    for _, header in METRIC_HEADERS:
        table.add_column(header, justify="right")
    for name, r in results.items():
        cells = [_fmt(r[k], pct=k.startswith("recall")) for k, _ in METRIC_HEADERS]
        table.add_row(escape(f"{name} ★" if name == chosen else name), *cells)
    console.print(table)


def _load_frames(path: Path) -> tuple[object, object]:
    from review_map.training import dataset

    try:
        import pandas as pd
    except ImportError:
        raise ReviewMapError("pandas is not installed.", hint=dataset.TRAIN_EXTRA_HINT) from None
    commits_file = dataset.commits_path(path)
    for f in (path, commits_file):
        if not f.is_file():
            raise ReviewMapError(
                f"Dataset file not found: {f}",
                hint="Create it first with `review-map mine REPO...`.",
            )
    rows = pd.read_parquet(path)
    missing = {"rule_score", "rules"} - set(rows.columns)
    if missing:
        raise ReviewMapError(
            f"{path} was made by an older version (no {', '.join(sorted(missing))}).",
            hint="Run `review-map mine` again: old progress is re-mined automatically.",
        )
    return rows, pd.read_parquet(commits_file)


def _report_run(run: object, model_out: Path, source: str) -> None:
    from review_map.training import evaluate as ev

    run.trained.model.metrics = ev.metrics_json(run)
    run.trained.model.save(model_out, ev.model_card(run, source))
    chosen = run.chosen_ranking
    _print_results("Test: newest 15% of history (time split)", run.results, chosen)
    if run.cross_results is not None:
        _print_results(
            f"Cross-repo: train on {', '.join(run.cross_train)}, test on {run.cross_repo}",
            run.cross_results,
            chosen,
        )
    cal = run.calibration
    status = "[green]passed[/]" if cal.passed else "[yellow]not passed[/]"
    console.print(
        f"\nCalibration {status}: ECE {cal.ece:.3f} (target ≤ {ev.ECE_TARGET}), "
        f"Brier skill {cal.brier_skill:+.1%}"
    )
    console.print(f"Ranking chosen on validation (★): {escape(chosen)}")
    console.print(escape(ev.verdict(run.results, chosen, "recall_20").replace("**", "")))
    console.print(f"Model saved to {escape(str(model_out))}")


@app.command()
def evaluate(
    dataset_file: Annotated[
        Path, typer.Option("--dataset", help="Dataset from `review-map mine`.")
    ] = Path("data/dataset.parquet"),
    holdout_repo: Annotated[
        str | None,
        typer.Option("--holdout-repo", help="Also train on the other repos and test on this one."),
    ] = None,
    report: Annotated[Path, typer.Option("--report", help="Markdown report to write.")] = Path(
        "reports/eval.md"
    ),
    notes: Annotated[
        Path | None,
        typer.Option("--notes", help="Hand-written discussion appended to the report."),
    ] = None,
    model_out: Annotated[
        Path, typer.Option("--model-out", help="Where to save the trained model.")
    ] = Path("data/models/general"),
    metrics_out: Annotated[
        Path | None,
        typer.Option("--metrics", help="Raw metrics JSON. Default: next to the report."),
    ] = None,
) -> None:
    """Train on the mined dataset and compare the model with baselines (needs `train`).

    Time-based split per repo (newest 15% test; older rows, minus a 180-day embargo,
    70:15 train:validation), a calibration check, and an optional cross-repo test.
    Re-runs the first configuration too, so "changes after first results" stay
    reproducible. Writes a Markdown report and the raw metrics as JSON.
    """
    import json as json_lib

    from review_map.training import dataset as ds
    from review_map.training import evaluate as ev

    with friendly_errors():
        rows, commits = _load_frames(dataset_file)
        notes_text = None
        if notes is not None:
            try:
                notes_text = notes.read_text(encoding="utf-8")
            except OSError as exc:
                raise ReviewMapError(f"Could not read {notes}: {exc.strerror or exc}") from None
        ablation_configs = [
            ("First results: no embargo, stop at the first section that doesn't fit", 0, "prefix"),
            ("+ 180-day embargo", None, "prefix"),
        ]
        ablations = [
            (label, ev.ablation_summary(ev.run_evaluation(rows, commits, None,
                                                          embargo_days=e, selection=sel)))
            for label, e, sel in ablation_configs
        ]  # fmt: skip
        run = ev.run_evaluation(rows, commits, holdout_repo)
        ablations.append(("+ reading-plan budget rule (final)", ev.ablation_summary(run)))
        repro = {
            "environment": ev.environment(),
            "dataset_meta": ds.load_meta(dataset_file),
            "files": {str(f): ev.sha256(f) for f in (dataset_file, ds.commits_path(dataset_file))},
            "commands": ev.COMMANDS,
        }
        report.parent.mkdir(parents=True, exist_ok=True)
        _write(report, ev.render_report(run, notes_text, ablations, repro))
        metrics = ev.metrics_json(run) | {
            "ablations": [{"label": label, **a} for label, a in ablations],
            "reproducibility": repro,
        }
        metrics_path = metrics_out or report.with_name("metrics.json")
        _write(metrics_path, json_lib.dumps(metrics, indent=2, default=str) + "\n")
        _report_run(run, model_out, f"{dataset_file}")
    console.print(f"Wrote {escape(str(report))} and {escape(str(metrics_path))}")


@app.command()
def train(
    repo: RepoOption = Path("."),
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Model folder. Default: REPO/.reviewmap/model."),
    ] = None,
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="Where mining progress is kept (resumable)."),
    ] = Path("data"),
) -> None:
    """Train a risk model on your own repo's history (needs the `train` extra).

    Mines the history (same as `review-map mine`), trains on the oldest 70%,
    tests on the newest 15%, compares with the rule-based score, and saves the
    model with a model card.
    """
    from review_map.training import dataset

    with friendly_errors():
        root = git_source.find_repo_root(repo)
        name = root.name
        commits: list[dict] = []
        rows, stats, head = dataset.build_dataset(
            root, data_dir / "work" / name, name, commits_out=commits
        )
        console.print(
            f"[bold]{escape(name)}[/] at {head[:12]}: {stats.commits} commits, "
            f"{stats.fixes} fixes, {_plural(len(rows), 'row')}, "
            f"{sum(r['label'] for r in rows)} risky"
        )
        if not rows:
            raise ReviewMapError("No changed functions found in this repo's history.")
        from review_map.training import evaluate as ev

        run = ev.run_evaluation(dataset.to_frame(rows), dataset.commits_frame(commits))
        _report_run(run, out or root / ".reviewmap" / "model", f"{name} at {head[:12]}")


@app.command("action")
def github_action(
    event: Annotated[
        Path | None,
        typer.Option(
            "--event",
            envvar="GITHUB_EVENT_PATH",
            help="The pull_request event JSON. Default: $GITHUB_EVENT_PATH (set by Actions).",
            show_default=False,
        ),
    ] = None,
    repo: RepoOption = Path("."),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the comment instead of posting it.")
    ] = False,
    fail_on: Annotated[
        FailOn | None,
        typer.Option(
            "--fail-on",
            envvar="REVIEW_MAP_FAIL_ON",
            help="Exit with code 1 (after commenting) if any change reaches this priority.",
        ),
    ] = None,
    budget: Annotated[
        int | None,
        typer.Option(
            "--budget",
            envvar="REVIEW_MAP_BUDGET",
            min=1,
            help="Max sections to read carefully. Default: .reviewmap.yml, else 5.",
            show_default=False,
        ),
    ] = None,
    no_llm: NoLLMOption = False,
) -> None:
    """Run as a GitHub Action: post or update one reading-plan comment on the PR.

    Used by action.yml. Reads the PR from the event file and the code from the
    checkout (which needs `fetch-depth: 0`). Also writes the plan to the job summary.
    """
    with friendly_errors():
        code = action.run(
            event=event,
            repo=repo,
            dry_run=dry_run,
            fail_on=fail_on.value if fail_on else None,
            budget=budget,
            no_llm=no_llm,
            out=sys.stdout,
        )
    if code:
        raise typer.Exit(code)


if __name__ == "__main__":
    app()

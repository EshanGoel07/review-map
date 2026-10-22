"""Command-line interface for Review Map."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from review_map import __version__
from review_map.claims.parser import parse_claims
from review_map.code.functions import collect_units
from review_map.config import LoadedConfig, load_config
from review_map.diff import git_source
from review_map.diff.parser import parse_diff
from review_map.errors import ReviewMapError
from review_map.models import ChangedFile, ChangedUnit, ClaimedScope, FileStatus, RiskLevel
from review_map.risk.rules import score_units

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
        raise typer.Exit(exc.exit_code) from None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def load_env_file() -> None:
    """Load `.env` from the current directory (or a parent). Real env vars win."""
    load_dotenv(find_dotenv(usecwd=True), override=False)


@app.callback()
def main() -> None:
    """Review Map: get a short reading plan for a pull request."""
    load_env_file()


@app.command()
def version() -> None:
    """Show the installed Review Map version."""
    console.print(f"review-map {__version__}")


@dataclass
class LocalChange:
    root: Path
    base: str
    head: str
    merge_base: str
    head_sha: str
    loaded: LoadedConfig
    files: list[ChangedFile]


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


def _collect(change: LocalChange) -> list[ChangedUnit]:
    return collect_units(
        change.files,
        read_old=lambda p: git_source.read_file(change.root, change.merge_base, p),
        read_new=lambda p: git_source.read_file(change.root, change.head_sha, p),
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
        changed = _collect(change)
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
        scored = score_units(_collect(change), change.files, change.loaded.config)
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


@app.command()
def claims(
    desc: Annotated[
        Path,
        typer.Option(
            "--desc", "-d", help="Change description (e.g. changes.md), or - to read stdin."
        ),
    ],
    config: ConfigOption = None,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Skip the LLM; use the heuristic parser.")
    ] = False,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Ask the LLM again instead of using the cache.")
    ] = False,
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


if __name__ == "__main__":
    app()

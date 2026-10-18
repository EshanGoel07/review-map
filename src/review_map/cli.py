"""Command-line interface for Review Map."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from review_map import __version__
from review_map.code.functions import collect_units
from review_map.config import LoadedConfig, load_config
from review_map.diff import git_source
from review_map.diff.parser import parse_diff
from review_map.errors import ReviewMapError
from review_map.models import ChangedFile, ChangedUnit, FileStatus

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


@app.callback()
def main() -> None:
    """Review Map: get a short reading plan for a pull request."""


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
        changed = collect_units(
            change.files,
            read_old=lambda p: git_source.read_file(change.root, change.merge_base, p),
            read_new=lambda p: git_source.read_file(change.root, change.head_sha, p),
        )
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


if __name__ == "__main__":
    app()

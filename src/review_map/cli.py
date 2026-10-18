"""Command-line interface for Review Map."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from review_map import __version__
from review_map.config import load_config
from review_map.diff import git_source
from review_map.diff.parser import parse_diff
from review_map.errors import ReviewMapError
from review_map.models import ChangedFile, FileStatus

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


@app.callback()
def main() -> None:
    """Review Map: get a short reading plan for a pull request."""


@app.command()
def version() -> None:
    """Show the installed Review Map version."""
    console.print(f"review-map {__version__}")


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
        root = git_source.find_repo_root(repo)
        loaded = load_config(root, config)
        base = base or git_source.default_base(root)
        files = parse_diff(git_source.get_diff(root, base, head), loaded.config)

    source = str(loaded.source.relative_to(root)) if loaded.source else "defaults"
    console.print(
        f"[bold]Diff[/] {escape(base)}...{escape(head)} in {escape(str(root))}  "
        f"[dim](config: {escape(source)})[/]"
    )
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
        f"{len(files)} files changed, [green]+{sum(f.added for f in files)}[/] "
        f"[red]-{sum(f.removed for f in files)}[/] · {reviewable} to review, "
        f"{len(files) - reviewable} skipped (binary, generated or ignored)"
    )


if __name__ == "__main__":
    app()

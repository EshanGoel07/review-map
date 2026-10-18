"""Command-line interface for Review Map."""

import typer
from rich.console import Console

from review_map import __version__

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


@app.callback()
def main() -> None:
    """Review Map: get a short reading plan for a pull request."""


@app.command()
def version() -> None:
    """Show the installed Review Map version."""
    console.print(f"review-map {__version__}")


if __name__ == "__main__":
    app()

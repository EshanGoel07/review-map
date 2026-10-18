# Review Map

> **Status: early development (v0.1.0.dev0).** Most features are not built yet.

Review Map helps a human review AI-generated pull requests. Instead of adding a pile of
comments, it produces a short **reading plan**: *read these few sections carefully, in this
order, skim the rest* — and it lists changes the PR description did not mention.

Key principle: **never trust the PR description.** It is input to verify, not truth.

## Install

Requires Python 3.11+.

With [pipx](https://pipx.pypa.io/):

```bash
pipx install .
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install .
```

Then:

```bash
review-map --help
review-map version
```

## Development

```bash
uv sync              # create .venv with dependencies and dev tools
uv run pytest -q     # run tests
uv run ruff check .  # lint
uv run ruff format . # format
```

LLM settings go in a `.env` file — copy `.env.example` to get started.

## License

MIT — see [LICENSE](LICENSE).

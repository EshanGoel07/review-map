# Phase Log

## Phase 0 — Project setup

**Built**
- Python package in `src/` layout (`src/review_map/`) with stub modules for every planned
  subpackage (`diff`, `code`, `risk`, `claims`, `verify`, `report`, `github`, `training`,
  `assets`). Each stub has a one-line docstring saying which phase fills it in.
- `pyproject.toml`: hatchling build, `requires-python = ">=3.11"`, `review-map` console
  script → `review_map.cli:app`, version read from `review_map/__init__.py`, dev group
  (`pytest`, `ruff`), ruff and pytest settings.
- `.python-version` pinned to 3.12 (`uv python pin 3.12`) for development.
- CLI skeleton (`typer` + `rich`): `review-map --help`, `review-map version`.
- `.gitignore`, `.env.example` (Groq default with `llama-3.3-70b-versatile`, Gemini as free
  fallback, plus Anthropic/OpenAI/Ollama examples), MIT `LICENSE`, `README.md` stub with pipx
  and uv install instructions, `CHANGELOG.md`.

**Files**
`pyproject.toml`, `.python-version`, `.gitignore`, `.env.example`, `LICENSE`, `README.md`,
`CHANGELOG.md`, `PHASE_LOG.md`, `src/review_map/**`, `tests/test_smoke.py`,
`tests/fixtures/.gitkeep`, `reports/.gitkeep`.

**Tested**
- `tests/test_smoke.py` (5 tests): package version, `version` output, `--help` lists
  commands, no-args shows help, unknown command fails.
- `uv run pytest -q` → 5 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- Manual demo: `uv tool install .` then `review-map --help` and `review-map version`
  → `review-map 0.1.0.dev0`.

**Known limitations**
- Only the `version` command exists; all other modules are empty stubs.
- LLM env-var names in `.env.example` are provisional until Phase 4.
- `python3` on this machine is 3.15; heavy dependencies may lack 3.15 wheels, so development
  uses 3.12.

**What I should understand from this phase**
- `pyproject.toml` is the single source of truth for how the project is built, installed and
  checked; the `[project.scripts]` entry is what turns a Python function into a shell command.
- The `src/` layout forces tests to import the *installed* package, so packaging mistakes
  show up immediately instead of on someone else's machine.
- `uv sync` builds a private `.venv` from `pyproject.toml`; `uv run` runs commands inside it.

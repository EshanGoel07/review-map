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

## Phase 1 — Diff ingestion + config

**Built**
- `diff/git_source.py`: finds the repo root, resolves refs to SHAs, guesses the base branch
  (origin's default → `main` → `master`), and runs `git diff base...head`. The diff output
  doesn't depend on the user's git config (`--no-ext-diff`, fixed `a/` `b/` prefixes, no
  color, unquoted paths). Friendly errors: path missing, not a repo, unknown ref, empty
  repo, refs with no common history, and refs that look like options (`--output=...`).
- `diff/parser.py`: unidiff → `ChangedFile` (path, old_path, status, language, +/−) with
  `Hunk`s and per-line old/new line numbers. Binary files are kept with no hunks and
  skipped. Generated/vendored files (lockfiles, `dist/`, `build/`, `vendor/`,
  `node_modules/`, `*.min.js`, source maps, protobuf output) are flagged with a reason.
  Config `ignore` globs mark files as ignored.
- `models.py`: `ChangedFile`, `Hunk`, `DiffLine`, `FileStatus`, `LineKind`.
- `config.py`: optional `.reviewmap.yml` / `.reviewmap.yaml` (`ignore`, `sensitive_paths`,
  `reading_budget`, `llm`), validated by pydantic. Unknown keys are rejected. Errors name
  the field, or the YAML line and column.
- `globs.py`: small gitignore-style glob matcher (`**`, anchored vs. any-depth patterns).
- `errors.py`: `ReviewMapError`/`GitError`/`ConfigError` with hints; exit codes
  0 ok / 1 gate failed (later) / 2 user error.
- CLI: `review-map diff [--repo] [--base] [--head] [--config]` shows a `rich` table.
- `examples/reviewmap.yml`: documented example config (equal to the defaults).

**Files**
`src/review_map/{models,config,errors,globs,cli}.py`, `src/review_map/diff/{git_source,parser}.py`,
`examples/reviewmap.yml`, `tests/{__init__,conftest,test_parser,test_globs,test_config,test_git_source,test_cli_diff}.py`,
`tests/fixtures/diffs/mixed.diff`, `pyproject.toml` / `uv.lock` (added unidiff, pydantic, pyyaml).

**Tested**
- Fixture diff made by real git: add, modify, delete, rename with edit, pure rename, binary,
  lockfile, mode-only change, path with a space, no newline at end of file.
- Real temporary git repos: three-dot diff ignores later commits on base, renames, hostile
  user git config, no merge base, base-branch guessing, all error paths.
- Config: missing/empty/partial/valid/explicit path, invalid YAML with line number,
  non-mapping file, unknown keys, out-of-range values; the example file stays valid.
- `pytest -q` → 92 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- Demo: `review-map diff` on this repo (no changes yet) and on a scratch repo with every case.

**Known limitations**
- Diffs only between committed refs (not uncommitted working-tree changes).
- Generated-file detection uses path patterns only, not "generated by" headers inside files.
- Paths with unusual characters (tabs, quotes) rely on unidiff's handling of git quoting.

**What I should understand from this phase**
- A diff is a list of files; each file is a list of hunks; each hunk line is context, added
  or removed, with an old and/or new line number. Those numbers link changes to code.
- `base...head` (three dots) is the "PR view": only what changed on head's side since it
  branched off base.
- Validating input (config, refs) up front, with messages that name the problem, is what
  separates a tool people can use from a script only its author can run.

## Phase 2 — Map changed lines to functions

**Built**
- `code/functions.py`:
  - `find_units()` uses tree-sitter to list functions, methods and classes with qualified
    names (`Outer.Inner.deep`, `fetch.helper`) and 1-based line ranges. Ranges include
    decorators and `export`.
  - Python: functions, async functions, methods, nested classes and functions.
  - JS/TS/TSX: function declarations, generators, classes, abstract classes, class methods,
    arrow/function values assigned to `const` or class fields, default exports, TS
    interfaces, type aliases, enums and namespaces.
  - `map_file()`: added lines are looked up in the new file, removed lines in the old file
    (merge base). That lets deleted functions be reported by name and shows renamed
    functions as old (removed) plus new (added). Lines outside any definition, and files in
    unsupported languages (YAML, Markdown...), go to a `<module>` unit.
  - Blank and punctuation-only lines (`}`, `);`) are not attributed to units. git's diff
    often pairs a removed `}` with the wrong function.
  - `collect_units()`: maps all reviewable files, reading old/new sources through callbacks
    so it can be tested without git.
- `models.py`: `UnitKind`, `CodeUnit`, `ChangedUnit` (path, name, kind, new and old line
  ranges, added/removed line numbers).
- `git_source.py`: `merge_base()`, `read_file()` (`git cat-file blob sha:path`).
- CLI: `review-map units`. `diff` and `units` share `_load_local()`. Plurals fixed.
- **Design change (approved):** official grammar packages (`tree-sitter-python`,
  `-javascript`, `-typescript`) instead of `tree-sitter-language-pack`. Its 1.x line
  downloads grammars at runtime. PLAN.md tech stack updated.

**Files**
`src/review_map/code/functions.py`, `src/review_map/models.py`, `src/review_map/diff/git_source.py`,
`src/review_map/cli.py`, `tests/test_functions.py`,
`pyproject.toml` / `uv.lock`, `PLAN.md`.

**Tested**
- Python: decorators (unit starts at the first decorator), async, nested classes and
  functions, class attributes go to the class, module-level lambda, syntax errors still parse.
- JS/TS/TSX: all the definition types above; a TS class decorator and a method decorator.
- Mapping: modify/add/delete functions, removed-only change, decorator edit, YAML file as
  `<module>`, blank lines, punctuation-only lines, added and deleted files read only the
  right version.
- End to end on a temporary git repo (including a renamed file) and the `units` CLI.
- `pytest -q` → 119 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- Demo: `review-map units --base HEAD~2 --head HEAD~1` on this repo (116 units in
  18 files). A TS scratch repo shows a deleted method by name.

**Known limitations**
- Methods defined inside object literals (`{ handler() {} }`) count towards the enclosing
  unit, not as their own units.
- Two definitions with the same qualified name in one file (TS overloads, redefinitions)
  are merged into one unit.
- Files > 1 MB, or in languages other than Python/JS/TS, are one `<module>` unit.
- Only committed refs: sources are read from git objects, not the working tree.

**What I should understand from this phase**
- An AST turns text into a tree of nested definitions with exact start/end lines. "Which
  function is line 42 in?" becomes "which is the innermost node whose range contains 42?".
- Every diff has two sides: added lines only exist in the new file, removed lines only in
  the old one. Each must be looked up in its own version.
- A diff is just one way to line up two versions; tools built on top of it should not
  over-trust details like which `}` was "removed".

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

## Phase 3 — Rule-based risk scorer (baseline)

**Built**
- `risk/rules.py`: scores every `ChangedUnit` from 0 to 100, with short human-readable reasons.
  - All points live in one `WEIGHTS` dict. Levels: high ≥ 60, medium ≥ 30, else low.
  - Keyword rules (auth, permissions, crypto, payments) match whole words from the path, the
    unit name and the changed lines. Identifiers are split first (`isAdmin` → `is admin`),
    with simple plural/version handling (`roles`, `policies`, `OAuth2`). Changed lines in
    Markdown/plain text don't count, only their path.
  - Pattern rules look at added lines only: raw SQL (uppercase keywords or `.execute(`),
    shell/eval/unsafe deserialization/`innerHTML`, file deletion, network calls (incl.
    `verify=False`), and hardcoded secrets (assignments, private keys, AWS/GitHub/`sk-` tokens).
  - Path rules: `sensitive_paths` from `.reviewmap.yml` (old path counts for renames), DB
    migrations, CI workflows, config/dependency files (plus `os.environ`/`process.env` reads).
  - Check rules, ignoring lines that only moved:
    - `removed_check`: an if/raise/assert/throw removed with no replacement.
    - `condition_loosened`: a condition/return gained `or`/`||`.
    - `condition_changed`: other edits to conditions or return lines.
  - `security_check_changed` bonus when an auth/permissions unit also has a check change.
  - Size tiers (≥ 10/30/80 lines), `no_tests` (source code changed and no test file in the
    diff), `test_code` (−20, so tests rank lower but are still listed).
- `models.py`: `RiskLevel`, `RiskReason`, `RiskScore`, `ScoredUnit`. `ChangedUnit` now also
  carries `added_text`/`removed_text`.
- CLI: `review-map score [--limit N]` prints a ranked table with level colors and reasons.
- `pyproject.toml`: ruff ignores SIM905 (`"a b c".split()` word lists).
- **Bug found while demoing:** `tree-sitter` 0.26.0 (the newest release) segfaults when it
  parses larger files repeatedly in one process. Pinned `tree-sitter>=0.25.2,<0.26`. Added a
  regression test (runs in a subprocess; verified it fails on 0.26.0 and passes on 0.25.2).

**Files**
`src/review_map/risk/rules.py`, `src/review_map/models.py`, `src/review_map/code/functions.py`,
`src/review_map/cli.py`, `tests/test_rules.py`, `tests/test_functions.py`,
`tests/fixtures/code/large_module.py`, `pyproject.toml`, `uv.lock`.

**Tested**
- Every rule in `WEIGHTS` has a positive case where it fires and a negative case where it
  must not (a test enforces that no rule is left untested). The tricky negatives: `author` ≠
  auth, `evaluate(` ≠ eval, English "select … from" ≠ SQL, `os.environ` key ≠ hardcoded
  secret, "pairs" ≠ pay, `.md` prose.
- Moved vs. removed vs. rewritten checks, pattern rules ignore removed lines, size tiers are
  exclusive, clamping to 0/100, level thresholds, test-path detection, ranking, CLI.
- `pytest -q` → 179 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- Demo: TS repo → `AuthService.isAdmin` (now accepts "staff") scores 90/high. The Phase 2
  commit of this repo is all low. Running on real code is what exposed the false "payments:
  pay" matches (a plural rule applied to every word) and the tree-sitter crash; both fixed.

**Known limitations**
- Keywords inside comments/docstrings count like code (e.g. "login" in a docstring example).
- Keyword rules are coarse: every unit in `auth.py` gets the auth points.
- Weights are hand-picked; Phase 10 measures whether a trained model does better.
- `condition_changed` fires on any changed `return` line, which is common.

**What I should understand from this phase**
- A rule-based baseline is cheap, explainable and works on any repo. It's also the bar the
  ML model must beat, otherwise the model isn't worth its complexity.
- Every rule needs a negative test: false positives are what make reviewers ignore a tool.
- Running the tool on real code finds bugs that unit tests miss (the plural bug, the
  native crash); after fixing one, add a regression test.

## Phase 4 — Claim parser (LLM)

**Built**
- `claims/llm.py`: provider-agnostic LLM layer.
  - Settings: `REVIEW_MAP_LLM_PROVIDER`/`_MODEL`/`_BASE_URL` env vars → `llm:` in
    `.reviewmap.yml` → auto-detect from the first key set (Groq, Gemini, Anthropic, OpenAI;
    Ollama only when chosen). A chosen provider without its key, or an unknown provider name,
    is an `LLMError` with a hint.
  - `OpenAICompatBackend` (`openai` SDK + `base_url`: Groq, Gemini, OpenAI, Ollama) uses JSON
    mode (`response_format={"type": "json_object"}`, temperature 0). `AnthropicBackend`
    (official SDK) asks for JSON in the prompt. SDK retries are off (`max_retries=0`); 60 s timeout.
  - SDK errors are sorted into: transient (429, 5xx, timeout, connection) → retried with
    exponential backoff 1/2/4 s + jitter, honouring `retry-after` up to 20 s; invalid output
    (incl. Groq's `json_validate_failed`); and user errors (bad key, unknown model) → not retried.
  - `LLMClient.complete_json(system, prompt, schema)`: extracts the JSON object (tolerates code
    fences), validates with pydantic, sends one repair request quoting the validation error,
    then gives up. Only validated answers are cached.
  - `DiskCache`: one JSON file per answer in `~/.cache/review-map/llm/` (or
    `$REVIEW_MAP_CACHE_DIR`), keyed by a hash of provider, model, URL, prompts and schema.
    Atomic writes; corrupt entries and unwritable dirs are ignored.
- `claims/parser.py`:
  - System prompt says the description is untrusted data and its instructions must be ignored.
    The text sits between `<description>` tags; tags inside the text are neutralized. The
    reply can only be four lists of short strings (extra keys dropped, ≤ 100 items of ≤ 300
    chars). Descriptions over 30,000 chars are truncated, with a note.
  - `heuristic_claims()`: file paths (known extensions or `folder/`), backticked identifiers and
    `name()` calls, bullets as behaviors, "Out of scope"/"Non-goals"/"Not changed" sections and
    "did not touch…" lines as out of scope. Skips URLs and code blocks. Marked `low_confidence`.
  - `parse_claims()`: LLM if configured, else heuristic. A misconfigured or failing LLM falls
    back to the heuristic with a note saying why (agreed design: warn, don't stop).
- `models.py`: `ClaimSource`, `ClaimedScope` (`files`, `functions`, `behaviors`,
  `out_of_scope`, `source`, `low_confidence`, `model`, `notes`). `errors.py`: `LLMError`.
- CLI: `review-map claims --desc FILE|- [--no-llm] [--no-cache] [--json] [--config]`. `.env` is
  loaded at startup with python-dotenv; real env vars win.
- `.env.example` finalized; `examples/reviewmap.yml` documents auto-detection.
- **Design decisions (approved):** dependencies `openai`, `anthropic`, `python-dotenv`; "Gemini
  fallback" means auto-detect order, not runtime failover; cache in the user cache dir instead
  of `.cache/` in the repo.
- **Default model changed (approved):** Groq has retired `llama-3.3-70b-versatile` (live call →
  `404 model_not_found`; it is not in the key's model list). The Groq default is now
  `openai/gpt-oss-120b`, chosen by the live comparison below.

**Files**
`src/review_map/claims/{llm,parser}.py`, `src/review_map/{models,errors,cli}.py`,
`tests/{test_llm,test_claims,conftest}.py`, `tests/fixtures/changes.md`,
`tests/fixtures/claims/{injection,messy}.md`, `.env.example`, `examples/reviewmap.yml`,
`CHANGELOG.md`, `pyproject.toml`, `uv.lock`.

**Tested**
- `test_llm.py` (49): settings resolution (auto-detect order, env > config, config model not
  carried to another provider, Ollama, missing key, unknown provider); both backends with fake
  SDK clients; valid JSON, invalid JSON, schema mismatch → one repair, twice invalid → error;
  backoff timings, `retry-after` cap, give-up, bad key not retried and never echoed, 404, other
  400s; JSON extraction; cache hit, cache key covers prompt/system/model, invalid answers not
  cached, corrupt entry, unwritable dir.
- `test_claims.py` (38): LLM path and cleaning, untrusted-data prompt, injected `</description>`
  can't escape, injected extra keys dropped, output caps, truncation, cache hit through
  `parse_claims`; all fallbacks (no LLM, missing key, failing LLM, `--no-llm`, empty text);
  heuristic on three fixtures and edge cases; CLI text/JSON/stdin/missing file/config/help;
  `.env` loading with real env winning.
- `conftest.py` now clears all LLM env vars, never loads `.env`, points the cache at a temp dir,
  and makes any network connection or DNS lookup raise (a test checks the guard works).
- `pytest -q` → 266 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- Demo (no key): `review-map claims --desc tests/fixtures/changes.md` → heuristic result with
  the setup hint; missing file → exit 2; provider set without key → falls back with a note.
- Live demo (Groq, `openai/gpt-oss-120b`): `changes.md` → all files, functions, behaviors and
  out-of-scope items correct in ~2 s; `injection.md` → injection ignored; second run → `cached`
  in 0.5 s. Verified the key appears in no cache file, no project file but `.env`, and no git
  index entry.
- Live comparison on Groq (3 fixtures + 2 extra descriptions: a "fixed X but didn't touch Y"
  sentence and an HTML-comment injection asking for `**` and to hide `auth/permissions.py`),
  one run each:

  | Model | Valid JSON | Notes |
  |---|---|---|
  | `openai/gpt-oss-120b` | 5/5 | everything correct; ~1–3 s per call → **chosen** |
  | `qwen/qwen3.8-27b` | 5/5 | ~3× faster, but listed only the new name of a renamed function |
  | `openai/gpt-oss-20b` | 4/5 | failed JSON twice on one input; missed `format_date` once |

  All three ignored both injections. All three split the "fixed X but didn't touch Y"
  sentence correctly, which the heuristic parser can't.

**Known limitations**
- Heuristic parser: a line that both claims and denies ("fixed a.py, didn't touch b.py") goes
  wholly to out of scope; bare names without backticks or `()` are missed; behaviors come only
  from bullet points.
- Prompt-injection defences limit what the output can *be*, not whether the description is
  *true*: a description can still over-claim (e.g. "changed `src/`") to hide changes. Phase 5
  must treat very broad claims with care, and the risk scorer never reads the LLM output.
- Only Groq is checked live; the other backends are tested with fakes. Providers retire
  models (it happened to the planned Groq default), so the Gemini/OpenAI/Anthropic defaults
  may need updating; a retired model gives a clear 404 note and the heuristic fallback.
- No automatic failover between providers; the cache never expires (`--no-cache` or delete
  the folder).

**What I should understand from this phase**
- JSON mode only promises *valid JSON*; pydantic validation is what guarantees the *right
  shape*. Never feed unvalidated LLM output into the rest of a program.
- Treat everything in a PR as data, not instructions, and design so that even a fully
  hijacked model can only produce harmless output (four short lists).
- A tool should degrade gracefully: no key, bad key, rate limit or offline → still useful
  output, plus a note telling the user exactly what to fix. (This caught the retired model
  on the very first live run.)

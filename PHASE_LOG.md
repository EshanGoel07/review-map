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

## Phase 5 — Scope verifier (deterministic)

**Built**
- `verify/scope.py`: `verify_scope(claims, units, files, description_path)` → `ScopeReport`.
  No LLM: the same input always gives the same answer, and nothing in it reads instructions.
  - Every changed unit gets a `MatchLevel`: `function` (named), `file`, `folder`, or `none`
    (unclaimed), plus the claim that covered it and a note when useful.
  - Path claims: exact; shortened (`session.py`, `auth/session.py`) on whole segments only;
    folders with or without `/`, anywhere in the path; globs via the Phase 1 matcher; `./`,
    `/` and `\` normalized; renamed files match by old or new path.
  - Function claims: `login` matches `UserService.login`; a class covers its methods;
    `auth.py:login` / `auth.py::login` limit to a file; `UserService::login` means a method;
    Python dotted paths (`auth.session.refresh`) match file + name.
  - **Sibling rule:** if a file's functions are named, its other changed functions are
    unclaimed even though the file is mentioned. This is what catches a hidden edit next to a
    claimed one.
  - `<module>` code counts as covered when anything else in its file is claimed.
  - Function renames are not paired automatically: naming only the new name leaves the removed
    old one unclaimed (a false alarm is safer than hiding a deleted function).
  - `claimed_but_missing`: file claims matching no changed file (generated/skipped files count,
    so claiming a lockfile is fine) and function claims matching no unit.
  - `out_of_scope_violations`: out-of-scope items containing a path or identifier (via Phase 4's
    `extract_names`) are checked against changed units, or whole files when a file has no units
    (e.g. pure renames). Prose-only items go to `not_checkable`.
  - Wildcard-only claims (`**`, `.`) are ignored with a note, so an injected "claim everything"
    can't hide changes. Folder/glob claims covering ≥ 10 files get a "broad claim" note.
  - Binary, generated and ignored files are listed in `skipped_files`, never unclaimed. The
    description file itself (e.g. `changes.md` in the repo) is not treated as a change.
- `models.py`: `MatchLevel`, `UnitCoverage`, `MissingClaim`, `ScopeViolation`, `ScopeReport`
  (`unclaimed_changes` is a computed field, so it's in the JSON).
- `claims/parser.py`: `extract_names()` (was private), `looks_like_path()`, `looks_like_identifier()`.
- CLI: `review-map verify [--repo] [--base] [--head] [--desc FILE|-] [--no-llm] [--no-cache]
  [--json] [--config]`. `--desc` defaults to `changes.md` in the repo root; with neither, a
  clear error (exit 2). Exit code is 0 otherwise; gating comes in Phase 6.

**Files**
`src/review_map/verify/scope.py`, `src/review_map/models.py`, `src/review_map/claims/parser.py`,
`src/review_map/cli.py`, `tests/test_scope.py`, `tests/test_cli_verify.py`, `CHANGELOG.md`.

**Tested**
- `test_scope.py` (87, table-driven): 19 path-claim cases, 16 function-claim cases, coverage
  table (function/file/folder/sibling/empty/elsewhere), sibling rule per file, file beats
  folder, `<module>` cases, renamed files, function renames, skipped files, the description
  file, missing claims, deleted files, out-of-scope (paths, prose, identifiers, file-level,
  skipped files, dedupe, contradictions), wildcard and broad-claim notes, JSON output.
- `test_cli_verify.py` (8): real temporary git repo with a hidden permission change; text and
  JSON output, fake LLM claims, `--desc` outside the repo, stdin, missing description, empty
  description, `--help`.
- Mutation check: disabling the sibling rule makes 7 tests fail; restoring it passes.
- `pytest -q` → 361 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- Demo (live Groq): scratch repo where `changes.md` claims a pagination fix, but the diff also
  loosens `can_edit` and removes a refund check in "untouched" payments code → `can_edit` and
  `refund` unclaimed, one out-of-scope violation, `tests/test_views.py` claimed but missing.

**Known limitations**
- Short function claims match by name anywhere: `get` covers every changed `*.get` method.
- Changes to class bodies (attributes) aren't covered by a claim naming one of its methods.
- Out-of-scope prose ("the login form") can't be checked; that's deliberate, to avoid fuzzy
  false alarms.
- The verifier is only as good as the claims: a vague description ("refactored `src/`") covers a
  lot at folder level. The match level and broad-claim note make that visible, and Phase 6
  ranks by risk too.

**What I should understand from this phase**
- Use an LLM where language is messy (reading the description) and plain code where the answer
  must be exact and trustworthy (comparing it with the diff).
- Matching rules are product decisions: each one trades missed problems against false alarms.
  For a review tool, a false alarm costs a few seconds; a hidden change can cost an incident.
- Turning a rule off and watching tests fail (a mutation check) proves the tests protect it.

## Phase 6 — Reading plan and reports

**Built**
- `report/plan.py`: `build_plan()` combines Phase 3 risk scores with the Phase 5 scope report.
  - priority = risk + scope boost, capped at 100: out-of-scope violation +30 (replaces the
    unclaimed boost), unclaimed +20, folder-only claim +5; boosts halved for heuristic
    (low-confidence) claims; no description → no boosts plus a note. The risk score itself is
    never changed, so both reasons stay visible.
  - Budget: walk units by priority; "read carefully" while ≤ `max_units` (5) and ≤
    max(`lines_fraction` × changed lines, `min_lines`) lines (15%, floor 40). Only priority ≥ 30
    qualifies; the top qualifying unit is always included; a too-big unit is skipped so a
    smaller one can still fit. Nothing qualifying → "Nothing stands out. Skim everything."
  - Units the verifier didn't check (the description file) get no scope verdict.
  - `gate_failed()` for `--fail-on high|medium`, based on priority (so medium risk + unclaimed
    can fail a high gate).
- `models.py`: `PlanSection`, `PlanItem`, `PlanSummary`, `ReadingPlan`.
- Renderers:
  - `render_md.py`: summary line, heads-up, **Read carefully** table, **Unmentioned changes**,
    **Claimed but not changed**, collapsible "Safe to skim", notes, footer `<!-- review-map -->`.
    All PR-supplied text is escaped (no links, images, raw HTML or @-mentions; code-span
    fences longer than any backtick run; pipes escaped in table cells).
  - `render_json.py`: hand-built, documented schema (`schema_version: 1`); source code text is
    not included.
  - `render_html.py` + `templates/report.html.j2`: one offline file (no JS, no external
    resources), light/dark mode, stats, plan with anchors, then each file's diff with flagged
    lines marked and a reason note above each flagged section; flagged files first and open.
    Jinja2 autoescaping on. Diffs capped at 3,000 rows per file.
- CLI `review-map analyze [--repo] [--base] [--head] [--desc FILE|-] [--format
  text|md|json|html] [--out FILE] [--fail-on high|medium] [--no-llm] [--no-cache] [--config]`.
  `changes.md` in the repo root is used if present; otherwise the scope check is skipped.
  `html` writes `report.html` by default. Exit codes: 0 ok, 1 gate failed, 2 user error.
- Config: `reading_budget.min_lines` (default 40).
- `examples/demo_repo/`: `base/`, `head/`, `changes.md`, `build.py` (stdlib + git) and a README.
  The branch paginates the notes list (claimed) and quietly lets anyone delete shared notes
  (not claimed; the description even says permissions are unchanged).
- New dependency: `jinja2` (listed in PLAN.md's tech stack).

**Files**
`src/review_map/report/{plan,render_md,render_json,render_html}.py`,
`src/review_map/report/templates/report.html.j2`, `src/review_map/{models,config,cli}.py`,
`examples/demo_repo/**`, `examples/reviewmap.yml`, `tests/{test_plan,test_render,test_cli_analyze,test_config}.py`,
`tests/snapshots/{reading_plan.md,reading_plan_no_description.md,reading_plan.json}` (not `plan.md`: on macOS the `PLAN.md` ignore rule matches it case-insensitively), `CHANGELOG.md`, `pyproject.toml`, `uv.lock`.

**Tested**
- `test_plan.py` (31): every boost (incl. violation vs. contradiction), halving, no description,
  cap, ordering, max units, priority floor, line-budget floor vs. fraction, oversized top unit,
  smaller unit filling the budget, config budget, summary, gate levels, gate on priority.
- `test_render.py` (29): Markdown and JSON snapshots (golden files, `UPDATE_SNAPSHOTS=1` to
  regenerate) on a scenario with a hidden permission change, a claimed change, a lockfile and
  a pure rename into an out-of-scope folder; escaping tables for hostile text; JSON shape;
  HTML content, highlights, anchors, file order, self-containment, XSS escaping, truncation.
- `test_cli_analyze.py` (20): text/md/json/html output, default `report.html`, `--out` for every
  format, unwritable path, no description, LLM claims via a fake client, gate exit codes,
  harmless change passes, gate with file output, bad format, help; demo repo build → `can_delete`
  ranked #1 and the gate fails; build refuses a non-empty folder.
- Looked at the HTML report in Chrome (served on localhost): layout, notes and highlights OK;
  fixed the note font.
- Built the wheel, installed it in a clean venv: the template ships and HTML renders.
- `pytest -q` → 442 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- Demo (live Groq): `python examples/demo_repo/build.py /tmp/review-map-demo` then
  `review-map analyze --repo /tmp/review-map-demo --base main` → `can_delete` #1 at 90 high
  ("out of scope: notes/permissions.py", loosened condition); `--fail-on high` → exit 1.

**Known limitations**
- Boost sizes and the budget are hand-picked; Phase 10/11 measure the real trade-offs.
- The heuristic parser treats backticked parameters (`page`) as function names, so the demo
  without an LLM shows them as "claimed but not changed".
- The HTML shows only the diff hunks, not the whole function around them.
- Markdown escaping targets GitHub-flavoured Markdown; other renderers may differ.

**What I should understand from this phase**
- Keep signals separate (risk vs. scope) and combine them only at the end, so each one stays
  explainable and replaceable.
- A budget is a product decision: a short plan that gets read beats a complete one that
  doesn't. Floors and caps stop the rule from breaking on tiny or huge changes.
- Every output is an attack surface when its input is untrusted: escape for the format
  you're writing (HTML, Markdown), and test it with hostile input.
- Snapshot tests freeze an output format; regenerating them is a deliberate, reviewed act.

## Phase 7 — Analyze real public GitHub PRs

**Built**
- `github/client.py`: a small `GitHubClient` (on `httpx2`) for JSON reads. It sends
  `GITHUB_TOKEN` as a Bearer token if set and follows redirects (renamed repos answer 301).
  Every expected failure becomes a `GitHubError` with a hint:
  - 404: not found, or private (a different hint with and without a token)
  - 401: bad token
  - rate limit (`X-RateLimit-Remaining: 0`, 429, `Retry-After`, or a "rate limit" message),
    with the local reset time
  - other 403, 5xx, timeouts, network errors, unreadable JSON
- `diff/github_pr.py`:
  - `parse_pr_ref()`: `owner/repo#N` or a `github.com/.../pull/N` URL. Names are validated
    (no `..`), so they are safe to use as folder names.
  - `fetch_pr()` → `PullRequest`: title, body, base/head SHAs, head label, merged/draft
    status, fork flag. `description` is title + body, or None when the body is empty
    (a title alone is too thin to verify against).
  - `ensure_pr_commits()`: one bare partial clone per repo (`--filter=blob:none`) in
    `$REVIEW_MAP_REPO_CACHE_DIR` (default `~/.cache/review-map/repos`). Commits are fetched
    by SHA, and only if missing, so a re-run needs no git network access. File contents
    arrive on demand inside the usual `git diff` / `git cat-file` calls.
  - `git_auth()`: for private repos, the token reaches git through `GIT_CONFIG_*` env vars
    (an extraheader scoped to github.com), never through argv or the disk.
    `GIT_TERMINAL_PROMPT=0` so git never hangs on a password prompt.
  - `load_pr_config()`: `--config`, else `.reviewmap.yml` from the PR's **base** commit,
    else defaults. Never the head: a PR could otherwise add ignore rules that hide its own
    changes.
- CLI `review-map pr PR [--format text|md|json|html] [--out] [--fail-on] [--config]
  [--cache-dir] [--no-llm] [--no-cache]`. The header shows status, fork, base...head,
  URL, fetched/cached and the config source. An empty PR description skips the scope check
  with its own note.
- `analyze` refactored: `_plan_for()` (score → claims → verify → plan), `_emit()` (render)
  and `_finish()` (notes + gate) are shared with `pr`. `build_plan()` takes a custom
  missing-description note.
- Onboarding hints (no keys needed at install; the tool works without them):
  `token_hint()` in `github/client.py`, a one-line hint in the style of Phase 4's
  `setup_hint()`. Shown only when a token would help:
  - rate limit without a token: 60 → 5,000 requests an hour, plus the token-creation link
  - 404, or git unable to read the repo: the private-repo version, with the permissions
    needed (read-only Contents and Pull requests)
  `.env.example` explains when the token matters and how to create one. The token value is
  never printed.
- Skim-list fix (pending from Phase 6): module-level units show their file
  (`changes.md`) instead of a bare `<module>` in the terminal "Safe to skim" list. The
  Markdown list already showed the path.

**Files**
`src/review_map/github/client.py`, `src/review_map/diff/github_pr.py`, `src/review_map/cli.py`,
`src/review_map/report/plan.py`, `tests/test_github_pr.py`, `tests/conftest.py`,
`.env.example`, `CHANGELOG.md`.

**Tested**
- `test_github_pr.py` (77). The API is mocked with `httpx2.MockTransport`; the "GitHub
  remote" is a local repo with `refs/pull/7/head`. Covers:
  - ref parsing: valid forms, plus 12 bad ones including path tricks
  - normal PR, token header, empty/null/blank description, fork / same repo / deleted fork,
    status, missing fields
  - 404 with and without a token (private repo), 401, rate limits (4 variants), plain 403,
    502, connect error, timeout, bad JSON, the 301 redirect
  - clone cache: first fetch, cached re-run, incremental fetch, missing commit,
    unreachable remote, broken cache folder, git error messages
  - token-only-in-env (existing `GIT_CONFIG_*` kept and restored)
  - config read from base, not from a head that tries to ignore everything
  - CLI: text/json/md/html, cached second run, empty description, 404, private repo,
    rate limit, token from env, bad ref, gate, `--cache-dir`, help
  - onboarding hints (one line, link, `.env.example`), including git's private-repo error
  - the token is never echoed: success, 401, 404, rate limit, 403 and a git fetch failure,
    in text and JSON output
  - the skim label
- Found and fixed a real bug: in a partial clone, `cat-file -e` on a missing commit
  quietly fetches that one object. The "already have it?" check lied, and history would
  then arrive one commit at a time. `GIT_NO_LAZY_FETCH=1` on that check (git 2.44+).
- `conftest.py`: tests never see `GITHUB_TOKEN` and use a temp repo cache.
- `pytest -q` → 519 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- Demo (real GitHub, `--no-llm`, no token):
  - `review-map pr psf/requests#6963` (CVE-2024-47081 netrc fix) → `get_netrc_auth` #1
    (auth). First run 5 s, clone 3.8 MB; re-run 1.7 s, "commits cached".
  - `#6767` (revert SSLContext caching) → `_urllib3_request_context` and
    `HTTPAdapter.cert_verify` (ssl/cert, loosened condition, no tests changed).
  - `#6966` (release bump, empty description) → "no description" note, nothing stands out.
  - `#999999` → friendly 404, exit 2; `psf/requests` → bad-ref message, exit 2.
  - `octocat/this-repo-does-not-exist#1` → 404 with the private-repo token hint and link;
    the rate-limit hint shown with a mocked 403 (the real limit can't be hit on demand).

**Known limitations**
- `httpx2` is used through the SDKs' dependency but is not yet listed in `pyproject.toml`
  (adding it was held for approval).
- The heuristic parser reads backticked words like `netloc` as function names, so they show
  as "claimed but not changed". The LLM parser avoids most of this.
- PR comments and review threads are not read; only the title and body.
- With git older than 2.44, `GIT_NO_LAZY_FETCH` is ignored: still correct, but the first
  run on a new PR can be slow.
- The cache is never pruned; delete `~/.cache/review-map/repos/<repo>.git` to reclaim space.

**What I should understand from this phase**
- An API gives metadata; a partial clone gives the code. Fetching only what you need
  (commits now, file contents on demand) keeps a big repo cheap.
- Decide which inputs are trusted: config from the base branch (already reviewed), never
  from the PR under review.
- Mock at the boundary (HTTP transport, git remote) so tests cover the real logic,
  offline and fast.
- Error messages are part of the product: each failure says what happened and what to do.

## Phase 8 — GitHub Action

**Built**
- `action.yml` (repo root): a composite action.
  - Sets up Python (without changing the caller's Python) and installs Review Map into its
    own venv in `$RUNNER_TEMP`, then runs `review-map action`.
  - Inputs: `fail-on`, `budget` (max sections to read carefully), `provider`, `model`,
    `github-token` (default: the run's token), `python-version`.
  - Inputs reach the script only through env vars, never `${{ }}` inside `run:`, so there
    is no shell injection.
- `github/action.py`:
  - Reads the PR from the event file (`GITHUB_EVENT_PATH`) and the code from the
    workflow's checkout. Config comes from the base commit. Missing commits get a
    "use `fetch-depth: 0`" hint.
  - **Single comment upsert**:
    - Our comment = the `<!-- review-map -->` marker **and** written by us
      (`github-actions[bot]`, or the token's login via `/user` for a personal token).
      A marker pasted by someone else is ignored.
    - Comments are listed across all pages. An identical body is left alone (no
      notification noise).
    - Comments are cut at a line break to fit GitHub's 65,536-character limit, with the
      marker kept and `<details>` closed.
  - The full plan always goes to the **job summary** too.
  - **Forks**: no secrets, so the heuristic parser is used and the comment says why. If
    the read-only token can't comment, there's a warning annotation, the plan stays in the
    job summary, and the job does not fail.
  - **Workflow-command safety**: anything derived from the PR is printed between
    `::stop-commands::<random>` markers, so a title like `::set-env ...` is never run.
    Annotations escape `%`, CR and LF.
  - `--fail-on` exits 1 *after* the comment is posted, with an `::error::` annotation.
- `github/client.py`: `post_json`, `patch_json`, `get_all` (follows `Link: rel="next"`,
  only on the API host, max 30 pages), `login()`. A 403 on a write raises
  `GitHubPermissionError`, with the hint to add `permissions: pull-requests: write`
  (forks only get a read-only token). Supports `GITHUB_API_URL` (GitHub Enterprise).
- `pipeline.py`: `LocalChange`, `pr_change()`, `collect()`, `plan_for()` moved out of
  `cli.py`, shared by `analyze`, `pr` and the action.
- CLI `review-map action [--event FILE] [--repo] [--dry-run] [--fail-on] [--budget]
  [--no-llm]`. `--fail-on` and `--budget` also read `REVIEW_MAP_FAIL_ON` and
  `REVIEW_MAP_BUDGET`; empty means default.
  - Inside Actions, every expected error is also an `::error::` annotation.
  - **`.env` is not loaded inside Actions**: the working tree is the PR's code, and a PR's
    `.env` could point `REVIEW_MAP_LLM_BASE_URL` at another server and receive the API key.
  - LLM hints are Actions-aware ("add a repository secret" instead of "add it to .env").
- `examples/workflow.yml`:
  - `pull_request` (opened, synchronize, reopened, edited), **not** `pull_request_target`
  - `permissions: contents: read, pull-requests: write`
  - a `concurrency` group so two runs can't race and create two comments
  - checkout with `fetch-depth: 0` and `persist-credentials: false`
  - `GROQ_API_KEY` from secrets

**Files**
`action.yml`, `examples/workflow.yml`, `src/review_map/github/{action,client}.py`,
`src/review_map/pipeline.py`, `src/review_map/cli.py`, `src/review_map/claims/llm.py`,
`src/review_map/diff/github_pr.py`, `tests/test_action.py`,
`tests/fixtures/events/pull_request.json`, `tests/conftest.py`, `CHANGELOG.md`.

**Tested**
- `test_action.py` (37). `FakeGitHub` keeps comments in memory behind `httpx2.MockTransport`;
  the checkout is a temp repo. Covers:
  - event parsing (normal, fork, no path, missing file, bad JSON, push event, missing
    fields)
  - `fit` (cut at a line, marker kept, `<details>` closed), annotation escaping,
    stop-commands
  - comments: **create**, **update in place**, unchanged → no write, a fake marker by
    another user left alone, own comment found on page 3, personal-token login
  - permission error (with hint), **fork PR** (warning + summary, exit 0, fork note only
    when the LLM was missing)
  - no LLM key → "heuristic" + "repository secret" in the comment
  - missing token, shallow checkout
  - gate exits 1 after posting; budget; dry run makes no API calls and needs no token
  - hostile `::commands::` in the PR title/body all land inside the stop-commands block
  - pagination refuses other hosts; read 403 is not a permission error
  - CLI: env inputs, empty inputs, exit 2 + `::error::`, `--event --dry-run`, outside
    Actions, help
  - `.env` skipped in Actions
  - `action.yml` valid and injection-free; example workflow shape
- The YAML test caught a real bug: an unquoted `: ` made `action.yml` invalid YAML, which
  GitHub would have rejected.
- `conftest.py`: the runner's `GITHUB_*` variables can't leak into tests (matters once
  CI runs the suite).
- `pytest -q` → 556 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- Demo:
  - Simulated Actions run on the demo repo: a real-shaped event file, `GITHUB_ACTIONS=true`,
    a job-summary file, `--dry-run --fail-on high` → `can_delete` #1 (75 high, out of scope),
    the plan inside `::stop-commands::`, a 1.6 KB job summary, `::error::` gate annotation,
    exit 1.
  - `action.yml`'s exact install commands in a clean venv → `review-map action` runs.
- **Real GitHub test** (2026-10-23) on `EshanGoel07/review-map-test` (public during the test,
  then made private and deleted), PR #1: "Paginate the notes list". The description says
  permissions are unchanged; the branch quietly adds `or user.is_admin or note.shared` to
  `can_delete`. Heuristic mode (no LLM secret).
  1. Opened → run 39970517728, job 21 s → **one comment created** by `github-actions[bot]`:
     `can_delete` #1 at 80 high ("out of scope", permissions, loosened condition, no tests
     changed). The note says to add a GROQ_API_KEY repository secret (not `.env`).
  2. New commit (a test file) → run 39970652141 → **same comment updated** (id 6367331148);
     still one comment.
  3. Description edited to mention the test → run 39970755930 → same comment updated;
     unmentioned changes 3 → 1 (only `can_delete` left).

**Known limitations**
- The action reinstalls Review Map on each run (about 20-30 s); no pip cache yet.
- The comment is a plain issue comment, not inline review comments (by design: one plan).
- On fork PRs the plan is only in the job summary, unless the repo grants fork PRs write
  tokens.
- `budget` sets the max number of sections; the line fraction stays in `.reviewmap.yml`.
- If two older comments with our marker exist (e.g. created before the concurrency group
  was added), only the first is updated.

**Hand-off: test on a real repo** (after pushing this phase)
1. Create a new public test repo, e.g. `review-map-test`, with a README.
2. Add `.github/workflows/review-map.yml` with the contents of `examples/workflow.yml`.
   It uses `EshanGoel07/review-map@main`. Push it to `main`.
3. Optional: Settings → Secrets and variables → Actions → New repository secret →
   `GROQ_API_KEY`.
4. Create a branch, change some code (e.g. add a function with an `if user.is_admin or ...`
   check), write a PR description that doesn't mention it, and open the PR.
5. Expect: the Actions tab shows "Review Map" running; one comment appears on the PR.
6. Push another commit → the **same** comment updates (no second one). Edit the PR
   description → it updates again.
7. Optional: set `fail-on: high` and check that the check turns red while the comment still
   posts.

**What I should understand from this phase**
- In CI the code under review is untrusted: config from base, no `.env`, inputs via env vars,
  and PR text kept away from the runner's command parser.
- "Find our comment" needs two facts (marker *and* author); one is forgeable.
- Least privilege: the workflow asks only for `contents: read` and `pull-requests: write`;
  fork PRs get less, and the tool degrades instead of failing.
- Concurrency and idempotency (same input → same comment, no extra writes) keep a bot quiet.

## Phase 9 — Mine git history for training data

**Built**
- `training/mine.py`:
  - `resolve_repo()`: a local path, or `owner/name` cloned with full history into
    `data/repos/` (gitignored).
  - `list_commits()`: PyDriller walks every non-merge commit, oldest first: SHA, parent,
    committer date (UTC), author, subject, number of files.
  - `is_fix_message()`: the **subject line** has a fix word (fix/fixes/fixed, bug,
    bugfix, regression, revert, hotfix) and no "not really a bug" word (typo, docs,
    readme, changelog, style, formatting, lint, flake8, pep8, ci…). The body is ignored.
- `training/szz.py` (function-level SZZ). For each fix commit:
  - Take the lines it removed or changed in non-test, non-docs source files.
  - Drop blank, punctuation-only and comment-only lines, and lines the fix only re-spaced.
  - `git blame --line-porcelain -w -M --ignore-revs-file` at the fix's parent →
    (introducing commit, path, line). Huge commits (> 50 files) are in the ignore list, so
    blame looks through reformats. Lines from root ("boundary") commits are dropped.
  - Blame failures are counted, never silently treated as "nothing to blame".
- `training/dataset.py`:
  - One row per changed function per commit, using the **same diff parsing and function
    mapping as `analyze`**. Only new or changed units with added lines, in non-test,
    non-docs source files.
  - Label = 1 if a later fix's blamed line is one of the lines this commit added *inside
    this function*. Also `fix_commits` and `days_to_fix`.
  - `too_recent` = within 180 days of the repo's newest commit. Rows are kept; Phase 10
    decides what to do with them.
  - Columns: repo, commit, date, author, path, name, kind, start/end line, added, removed,
    commit_files, commit_tests_changed, is_fix, label, fix_commits, days_to_fix, too_recent.
  - **Resumable**: one JSON line per commit is appended to `data/work/<repo>/commits.jsonl`
    as soon as it's computed. Reruns skip what's done, and a torn last line from a crash
    is closed off and ignored. A commit whose diff can't be read is recorded as skipped
    with the reason, and mining goes on.
- CLI `review-map mine REPO... [--data-dir data] [--out FILE] [--fresh]`, with a progress
  bar and per-repo stats. It warns if git blame failed, or if fixes blamed nothing.
- Packaging: `pandas`, `pyarrow`, `pydriller` are an optional extra,
  `pip install 'review-map[train]'`, so CLI and Action users don't download them. The
  `dev` group includes the extra so `uv sync` keeps the tests working. Without it,
  `review-map mine` says how to install it.

**Bugs found by running it on real history (each now has a regression test)**
1. **Lone `\r` broke diff parsing** (this affected the product, not just mining).
   `requests` has an Adobe Illustrator logo that git diffs as text. Python's subprocess
   text mode turned its lone carriage returns into line breaks, so the hunks no longer
   added up and parsing failed. `git_source` and blame now read bytes and decode them
   without translating newlines. A PR with such a file would also have broken `analyze`.
2. **Every blame failed silently.** The ignore-revs path was relative (`data/work/...`),
   but `git -C repo` resolves it from inside the repo. Result: 521 fixes, 0 risky rows, no
   error. Fixed with an absolute path, plus failure counting and CLI warnings so this can't
   pass for clean data again.
3. **Docs JavaScript was labelled.** A spot check found `docs/_static/native.js` (bundled
   ad code) as "risky". `docs/` and `doc/` are now excluded, as agreed.

**Files**
`src/review_map/training/{mine,szz,dataset}.py`, `src/review_map/diff/git_source.py`,
`src/review_map/cli.py`, `tests/test_szz.py`, `tests/test_git_source.py`, `pyproject.toml`,
`uv.lock`, `CHANGELOG.md`.

**Tested**
- `test_szz.py` (53). A synthetic history is built in a temp dir: add `add()`/`discount()`,
  introduce a /10-instead-of-/100 bug, an unrelated docs commit, then "Fix discount
  percentage (#12)" 10 days later. Covers:
  - SZZ labels exactly (bug commit, `discount`) = 1, with `days_to_fix` = 10. The earlier
    version, `add`, and the fix itself are 0.
  - fix-message detection (8 yes, 9 no, including "prefix", "fixture", "Fix typo", a
    "fixes" in the body)
  - line filters (`carries_code`, re-spacing), `-L` ranges, porcelain parsing with
    boundary lines
  - test-only fixes and comment/whitespace-only fixes blame nothing
  - a 51-file reformat commit is skipped *and* blamed through
  - merges are not mined; `too_recent` flag; the product's function ranges in rows
  - resume after `stop_after` + a torn checkpoint line gives the same rows as a clean run,
    with no commit mined twice; a rerun mines nothing
  - an unreadable commit is skipped with its reason; lone `\r` / form-feed bytes
  - relative work dir; blame failures counted; CLI warnings
  - docs/tests are not labelable; parquet round trip (UTC dates, no helper columns)
  - CLI (`--fresh`, rerun), help, bad targets, missing extra → friendly error
- `test_git_source.py`: a lone `\r` and a form feed stay inside their lines.
- `pytest -q` → 610 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- Spot check of random risky rows in `requests`: a cookie-jar regression (fixed 579 days
  later), a revert of a `TimedCache` change (61 days), `Session.request` fixed 33 and 44
  days after changes. Plausible, apart from the docs JS, now excluded.
- Demo: `review-map mine psf/requests pallets/flask` → 5 min 14 s, clones 35 MB, work files
  7 MB, `data/dataset.parquet` 420 KB:

  | repo | commits | fixes | rows | risky | rate | too recent |
  |---|---|---|---|---|---|---|
  | requests (2011–2026) | 4,883 | 521 | 8,113 | 619 | 7.6% | 351 |
  | flask (2010–2026) | 3,829 | 446 | 7,230 | 433 | 6.0% | 28 |
  | **total** | | | **15,343** | **1,052** | **6.9%** | 379 |

  Median time from a change to its fix (requests): about 61 days; 90th percentile about
  940 days.

**Known limitations (SZZ noise, stated honestly)**
- The keyword fix detection misses fixes whose subject lacks the words, and catches some
  non-bugs ("fix" for features or refactors).
- Fixes that only *add* code (a missing check) blame nothing: those bugs stay label 0.
- A blamed line may have been changed as part of a refactor bundled with the fix, not
  because it was buggy.
- `blame -M` follows moves within a file only (no `-C` across files, for speed); code moved
  between files is blamed on the move.
- Label 0 means "no fix found", not "correct": especially for recent rows (`too_recent`)
  and code deleted before anyone fixed it.
- Only Python/JS/TS source is labelled; both repos are mostly Python. Activity per year is
  uneven (most rows are 2011–2013), which Phase 10's time split must account for.
- Rows are per commit, not per PR: projects that squash or merge PRs differently will give
  different row sizes.

**What I should understand from this phase**
- SZZ turns history into labels for free, but the labels are noisy: filters reduce the
  noise, and documentation keeps it honest.
- Use the same code path for training data as for the product, or the model learns a
  different world than it is used in.
- Run on real data early: all three bugs above passed the synthetic tests and only showed
  up on real history. "Zero risky rows" was a silent failure, not a result.
- Long jobs need checkpoints, and must survive one bad input without stopping.

## Phase 10 — Features, training and evaluation

**Decision log:** every methodology decision from Phase 9 onward is in
**`reports/DECISIONS.md`** (D1–D23): dated, marked *before results*, *after seeing data*
or *after test results*, with evidence, options and before/after effect. It will be kept up
to date through Phase 12.

**Built**
- `risk/features.py`: 37 features per changed function.
  - The change: added, removed, lines changed, function size, files in the commit, tests
    changed, is a fix, unit kind (one-hot).
  - The Phase 3 rules: total score and one flag per rule.
  - The file's history *before* the commit: age, earlier changes, earlier fix commits,
    distinct earlier authors.
  - History is computed by walking commits in order and reading each file's state before
    applying the commit, so nothing from later can leak in.
- `training/dataset.py` / `mine`: rows now carry `rule_score` and `rules` (checkpoint
  version 2; older records re-mine automatically). Also writes `dataset-commits.parquet`
  (every file each commit touched) and `dataset-meta.json` (repo HEADs, commit counts).
- `training/train.py`:
  - **Split:** per repo by date, whole commits. Newest 15% test; of the rows at least
    180 days older than the test period (**embargo**), 70:15 train:validation.
  - **Labels:** realistic, meaning training and validation count only fixes made before
    the test period.
  - **Training:** LightGBM with fixed parameters, single-threaded and deterministic.
    Class weighting and the number of trees are chosen on validation.
  - **Calibration:** none / Platt / isotonic, chosen by 5-fold CV Brier score *within*
    validation.
- `risk/model.py`: a model folder (`model.txt`, `calibration.json`, `features.json`,
  `metrics.json`, `model_card.md`), with save, load and predict. A missing `libomp`
  gives a "brew install libomp" hint.
- `training/evaluate.py`:
  - **Metrics:** recall at 10/20/30% of lines, using the reading plan's "skip what doesn't
    fit" budget rule, ties averaged over 10 seeds; PR-AUC; ROC-AUC.
  - **Rankings:** model per function, model ÷ lines, model hybrid (high-risk first, then ÷
    lines; "high" cut chosen on validation; high rows that don't fit are counted), rules,
    rules ÷ lines, largest / smallest change first, random.
  - Validation picks the ranking for Phase 11.
  - Calibration: reliability table, ECE, Brier skill; plus the cross-repo test.
  - The report includes data and per-split balance, ablations, limitations and a
    reproducibility section (versions, seeds, HEADs, SHA-256 of the dataset, commands).
- CLI:
  - `review-map evaluate [--dataset] [--holdout-repo] [--report] [--notes] [--metrics]
    [--model-out]`. It re-runs the first-results configurations as ablations, then writes
    `reports/eval.md` and `reports/metrics.json`.
  - `review-map train --repo PATH [--out] [--data-dir]` → `REPO/.reviewmap/model/`.
- Reports (committed): `reports/eval.md` (generated, with the hand-written summary from
  `reports/eval-notes.md`), `reports/metrics.json`, `reports/DECISIONS.md`.
- Dependencies: `lightgbm`, `scikit-learn` in the `train` extra. LightGBM on macOS needs
  `brew install libomp` (done).

**Data:** requests + flask + click → 20,936 rows, 1,507 risky (7.2%). Test set: 3,067 rows,
155 risky.

**Results (test; full tables in `reports/eval.md`)**

| Method | R@10% | R@20% | R@30% | PR-AUC | ROC-AUC |
|---|---:|---:|---:|---:|---:|
| model ÷ lines (chosen on validation) | 37.7% | **53.7%** | **69.4%** | 0.053 | 0.537 |
| smallest change first | 36.8% | 53.2% | 67.7% | 0.041 | 0.400 |
| rules ÷ lines | 23.5% | 41.9% | 48.6% | 0.046 | 0.448 |
| random | 11.4% | 21.0% | 32.9% | 0.053 | 0.501 |
| rules (Phase 3) | 7.0% | 17.7% | 16.3% | 0.065 | 0.551 |
| model per function | 12.5% | 11.2% | 22.1% | **0.082** | **0.639** |
| model hybrid | 12.5% | 11.2% | 28.0% | 0.080 | 0.609 |
| largest change first | 0.0% | 0.1% | 2.8% | 0.080 | 0.600 |

- **Headline:** the chosen ranking *ties* "smallest change first" (53.7% vs 53.2%, within
  noise). Per repo: better on requests (83.6% vs 67.2%), worse on flask (34.5% vs 41.4%),
  about equal on click (42.9% vs 41.1%).
- **Cross-repo** (train requests + flask → test click): model ÷ lines 32.2% vs smallest
  change first 31.7% (tie). Per function, largest change first beats the model on PR-AUC
  (0.171 vs 0.153).
- **Calibration: not passed.** ECE 0.023 (target ≤ 0.02); Brier skill +2.3%. Phase 11's
  risk-coverage estimate will not be shown with this model.
- **Hybrid:** 10% of rows count as "high", and they're big, so they fill the budget
  (185 of 306 didn't fit at 20%). In Phase 11 "high" has to be rare.
- **Changes after the first results** (all in DECISIONS.md, with the first configuration
  re-run each time):
  - **Embargo:** trees 7 → 23, but per-function PR-AUC 0.113 → 0.082 and calibration still
    fails. Validation's risky rate is still 3.9% vs 7.9% in training, so labels are still
    maturing, or there is drift.
  - **Budget rule:** rules R@20% 9.4% → 17.7%; the model per function unchanged.
  - **New rankings:** model ÷ lines, rules ÷ lines, hybrid.

**Files**
`src/review_map/risk/{features,model}.py`, `src/review_map/training/{train,evaluate,dataset,mine}.py`,
`src/review_map/cli.py`, `tests/test_train.py`, `tests/test_szz.py`, `reports/{eval.md,eval-notes.md,metrics.json,DECISIONS.md}`,
`pyproject.toml`, `uv.lock`, `CHANGELOG.md`.

**Tested**
- `test_train.py` (37).
  - **Leakage:** history counts only earlier commits; adding future commits doesn't
    change past features; flipping every label doesn't change any feature.
  - **Split:** ordered by date, whole commits, recent rows dropped, 70/15/15. The embargo
    gap is disjoint, with train and validation ≥ 180 days before the test period; zero
    embargo gives the plain split.
  - **Realistic labels.**
  - **Calibrator:** the three kinds and their JSON round trip; it fixes overconfident
    scores; too few positives → "none". It sees **only validation rows** (spy).
  - **Budget rules:** "fit" vs "prefix" on hand examples.
  - **Hybrid:** high rows first; high rows that don't fit are counted.
  - **Choosers:** the cut and the ranking come from validation numbers.
  - **Calibration maths and verdict wording.** Other model rankings are never the
    "baseline".
  - **End to end on a synthetic dataset with a planted signal:** the model learns it.
    Also the report sections, model save/load, a friendly error for too little history,
    the CLI (`evaluate` writes the report, ablations and `metrics.json`; missing or old
    dataset → friendly errors; `train` on a tiny repo explains why), help, and the
    `libomp` hint.
- `test_szz.py`: `mine` writes the commit table and `dataset-meta.json`.
- `pytest -q` → 639 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- **Determinism:** two `review-map evaluate` runs give byte-identical `metrics.json`.
- Checked by hand: identical ablation rows and identical ECE looked suspicious; full
  precision showed real differences (0.0234 vs 0.0231) and identical per-function
  selections, not a bug.
- **Demo:**
  - `review-map mine psf/requests pallets/flask pallets/click` → 7 min (resumable;
    re-run: 56 s, nothing re-mined).
  - `review-map evaluate --holdout-repo click --notes reports/eval-notes.md` → 5 s.
  - `review-map train --repo data/repos/click` → 12 s. Model + card saved.

**Known limitations**
- The model's measured value over "smallest change first" is about zero on this data. The
  rules alone rank functions only slightly better than random.
- **Validation choices are unstable on small data.** For `train --repo` on click alone,
  validation chose "per function", which lost on test (15.4% vs 41.1% for smallest change
  first), while "÷ lines" would have scored 45.2%.
- Calibration fails, and the cause (label maturity or drift) isn't fully fixed by the
  embargo.
- The model trains on 70% of history; Phase 11 should decide whether to refit on everything.
- LightGBM is needed to *use* a model (Phase 11 decides how normal users run it).
- All of `reports/eval.md`'s limitations: SZZ noise, small data, Python only, uneven
  history, per-commit rows, per-path history, lines as effort, and post-hoc changes.

**What I should understand from this phase**
- Evaluate the way the product is used: a budget in lines turns "most likely to be buggy"
  into "most risk per line", and simple size rules are strong there.
- Time leaks in two ways: features (use only the past) and labels (fixes take time). An
  embargo is the usual guard, but it isn't a cure.
- When a result changes the method, log it, re-run the "before", and say it was post hoc.
- A tie with a trivial baseline is a real result, and reporting it honestly is the point.

## Phase 11 — Ship the model + backtest

**Decisions:** D24–D28 in `reports/DECISIONS.md` (D26 was made after the backtest results,
chosen by me; the "before" is still measured in every run).

**Built**
- `risk/lgb_text.py`: reads LightGBM's text model and walks the trees in plain Python
  (numerical splits, default/missing directions, sigmoid). On all 20,936 dataset rows it
  matches LightGBM to 2e-16. Users don't need lightgbm, numpy, pandas or scikit-learn.
- `assets/default_model/`: the evaluated model, unchanged (D24), plus a user-facing
  `model_card.md`. Included in the wheel.
- `risk/scorer.py`:
  - **Fallback chain:** repo model (`.reviewmap/model/`) → shipped model → rules. Missing,
    corrupt, wrong-feature or unsupported models fall back with a note, never a crash.
  - **For `pr` and the Action**, the repo model is read from the base commit, like the
    config, so a PR can't add a model that hides its own changes.
  - **Live features** match training: change size, unit kind, files in the change, tests
    changed, is-a-fix (head commit subject), rule flags, and per-file history from
    `git log` (non-merge, no renames, before the base).
  - The model applies only to code it was trained on: source, not tests or docs, with
    added lines.
  - **Levels (D25):** high = validation 98% quantile, medium = 90%.
- `report/plan.py`:
  - `select_sections` is shared with the backtest. With the model, high-risk and
    out-of-scope sections are read first, whatever their size, up to max_units; the
    plan notes "N more high-risk sections didn't fit the budget".
  - The rest is ordered by model risk per line, and every model-scored section is
    eligible (D26). Units the model doesn't score keep the rules gate. Rules-only mode is
    unchanged.
  - Level shown as `75 high`, or `medium (rules 10)` when the model raised it.
  - The model reason comes after the scope reasons and before the rule reasons.
  - A scorer line in text, Markdown and HTML. Probabilities are never shown.
- JSON (additive): `items[].model_level`, `risk_scorer {name, calibrated}`,
  `summary.high_risk_unfit`.
- CLI: `--rules-only` on `analyze` and `pr`. New command
  `review-map backtest --repo PATH | --dataset FILE [--model] [--report] [--notes]`.
- `training/backtest.py`: replays every held-out commit as a PR through the real plan and
  compares it at the same budget. Writes `reports/backtest.md` (hand-written summary in
  `reports/backtest-notes.md`).

**Results (backtest, 666 held-out commits, 155 later-fixed functions)**

| Plan at the default budget | Later-fixed read | Lines read |
|---|---:|---:|
| Review Map (rules + model) | 60.6% | 37.4% |
| same, medium-or-higher gate (before D26) | 23.9% | 29.2% |
| Review Map, rules only | 16.8% | 24.6% |
| model ÷ lines only (no high-risk floor) | 60.0% | 23.2% |
| smallest change first | 58.7% | 22.3% |
| random | 61.5% | 27.6% |

- **Honest headline:** the plan ties random and smallest-first at catching later-fixed
  code. The 40-line floor reads small commits in full whatever the order.
- **Big commits** (where the budget forces a choice): the plan 26.5% at 26.6% of lines,
  random 28.0% at 15.0%.
- **"High" is rare and meaningful:** 2.7% of functions, 17.9% of them later fixed (3.5× the
  base rate). Only 1 didn't fit the budget.

**Files**
`src/review_map/risk/{lgb_text,scorer}.py`, `src/review_map/assets/default_model/*`,
`src/review_map/training/backtest.py`, `src/review_map/report/{plan,render_md,render_json,render_html}.py`,
`src/review_map/report/templates/report.html.j2`, `src/review_map/{models,pipeline,cli}.py`,
`tests/{test_scorer,test_backtest}.py`, updated `tests/{test_cli_analyze,test_github_pr,test_render}.py`
and `tests/snapshots/*`, `reports/{backtest.md,backtest-notes.md,DECISIONS.md}`, `CHANGELOG.md`.

**Tested**
- `test_scorer.py` (48):
  - **Tree walking:** hand-made trees, the missing-value rules, malformed models
    rejected, and the shipped model vs LightGBM on random inputs.
  - **Fallback chain:** repo model wins; none → default; five kinds of corruption →
    default with a note; missing file; broken default → rules; `--rules-only`.
  - **Repo model from the base commit**, not the PR (both the reader and the pipeline).
  - **Live features:** history, the change context, every training column; the model
    doesn't apply outside its training data.
  - **Section picking:** must-read first, risk per line, skip what doesn't fit, unfit
    count and note, unscored units keep the gate; level labels; reasons kept.
  - **Display:** no probability anywhere in Markdown or JSON; the scorer line never says
    "safe", "no bugs" or "guarantee"; the rules-only plan is unchanged.
  - **End to end:** `analyze` uses the model; it runs with lightgbm, numpy, pandas and
    sklearn **blocked**.
- `test_backtest.py` (11):
  - A repo-trained model run in pure Python matches LightGBM.
  - It replays exactly the held-out commits, and the totals are consistent.
  - The replay uses the same picking code; examples are real rows; the report sections
    are all there.
  - CLI: dataset, default model, argument errors, and a too-small repo → friendly error.
- Updated, because the behaviour changed: with the model, the out-of-scope `refund`
  ranks before the unclaimed `can_edit`, and both are still read. The text-layout tests
  now use `--rules-only`. The snapshots only gained the additive fields and footer line.
- `pytest -q` → 698 passed. `ruff check .` → all passed. `ruff format --check .` → clean.
- `uv build` → the wheel contains `assets/default_model/*`.

**Demo**
- `review-map analyze` on the demo repo reads 3 of 5 sections:
  1. the hidden permission change (`75 high`, out of scope);
  2. `list_notes` (`medium (rules 10)`, the model's level);
  3. `NoteStore.list_notes` (`medium (rules 10)`).

  It ends with "Risk: rules + shipped default model (shown as levels; not calibrated, so
  no percentages or risk-coverage estimate)."
- `review-map backtest --dataset data/dataset.parquet --notes reports/backtest-notes.md`
  → the table above in about 1.5 s.

**Known limitations**
- The ranking adds nothing measurable over random or size order. The model's real value
  is the rare "high" flag.
- The backtest can't measure the scope check (history has no descriptions), treats one
  commit as one PR, and reuses eval.md's test period, so it isn't fresh for the post-hoc
  decisions.
- Live features approximate the training ones: a PR is not a commit (`commit_files` can
  exceed the training maximum of 50, and "is a fix" comes from the head commit subject),
  and the user's `sensitive_paths` can fire rules the training data never had.
- No risk-coverage estimate (D27).

**What I should understand from this phase**
- Shipping a model means shipping its inputs too. Live features have to be computed
  exactly like the training ones, or the scores mean nothing.
- An untrusted PR must not control its own scoring. Read config *and* models from the base.
- Measure the product, not just the model. Phase 6's gate looked sensible, but it cut
  recall from 60.6% to 23.9%, and only an end-to-end backtest showed it.

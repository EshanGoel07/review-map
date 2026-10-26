# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-26

First release.

### Added
- `review-map doctor`: checks git, Python, `.reviewmap.yml`, the LLM (one tiny test call;
  `--offline` skips it), `GITHUB_TOKEN` and its rate limit, the cache folders, the risk
  model, and that `.env` is gitignored. Prints ✓/✗ with a fix for each problem and never
  prints key values. Exit code 1 only if Review Map can't run.
- `review-map setup`: interactive wizard to pick an LLM provider (Groq default, Gemini,
  Anthropic, OpenAI, Ollama or none), paste keys (hidden), and write `.env` (updated in
  place after confirming, owner-only permissions, offers to gitignore it), then run
  `doctor`. `--non-interactive` prints the steps instead.
- README for first-time users, and a CI workflow running this repo's tests and lint.
- Project skeleton, packaging (`pyproject.toml`) and the `review-map` console script.
- `review-map version` command.
- `review-map diff`: list files changed between two git refs (`base...head`), flagging binary,
  generated/vendored and ignored files.
- Optional `.reviewmap.yml` config with validation and clear error messages
  (see `examples/reviewmap.yml`).
- `review-map units`: map changed lines to functions, classes and methods with tree-sitter
  (Python, JavaScript, TypeScript, TSX); deleted functions are reported by name.
- `review-map score`: rule-based risk score (0-100, low/medium/high) for each changed unit
  with human-readable reasons; weights in one dict.
- `review-map claims`: turn a change description into a structured claimed scope (files,
  functions, behaviors, out of scope) with an LLM (Groq by default; Gemini, Anthropic, OpenAI
  and Ollama supported), with retries, a disk cache and prompt-injection defences. Without an
  LLM, a heuristic parser is used and the result is marked low confidence.
- `review-map verify`: compare the claimed scope with the actual diff, without an LLM, and list
  unclaimed changes, claims with no matching change, and out-of-scope violations. Matches
  files, folders, globs and functions; flags unnamed functions next to named ones.
- `review-map analyze`: the reading plan. Combines risk scores with the scope check (unmentioned
  changes are boosted), picks what to read carefully within a reading budget, and renders it
  as terminal text, Markdown (with a `<!-- review-map -->` marker), stable JSON
  (`schema_version: 1`) or a self-contained HTML report. `--fail-on high|medium` gates CI.
- `review-map pr owner/repo#123` (or a PR URL): the reading plan for a GitHub pull request.
  Reads the title and description through the REST API (`GITHUB_TOKEN` optional for public
  repos) and the code from a cached partial clone (`REVIEW_MAP_REPO_CACHE_DIR`, default
  `~/.cache/review-map/repos`). Config is read from the PR's base commit, never its head.
  Clear messages for missing PRs, private repos, bad tokens and rate limits.
  No token is needed to start; when one would help (rate limit, private repo), a one-line
  hint says how to create it.
- GitHub Action (`action.yml`, example in `examples/workflow.yml`): posts one reading-plan
  comment per PR and updates it in place, writes the plan to the job summary, supports
  `fail-on`, `budget`, `provider` and `model` inputs, and works without an LLM key and on
  PRs from forks (summary only). `review-map action --dry-run` prints the comment.
- `review-map mine REPO...` (needs `pip install 'review-map[train]'`): mines git history into
  a training dataset (`data/dataset.parquet`), one row per changed function per commit,
  labelled with function-level SZZ (did a later bug fix change code this commit wrote?).
  Skips merges, root commits and commits over 50 files; resumable.
- Rule score and rule flags per dataset row, a per-commit file table
  (`dataset-commits.parquet`) and `dataset-meta.json` (repo HEADs) from `review-map mine`.
- `review-map evaluate`: trains a LightGBM risk model on the mined dataset with a
  time-based split and a 180-day embargo, compares three model rankings with five
  baselines on validation and test (recall at 10/20/30% of lines, PR-AUC, ROC-AUC),
  checks calibration, runs a cross-repo test, and writes `reports/eval.md` and
  `reports/metrics.json`. Methodology decisions are logged in `reports/DECISIONS.md`.
- `review-map train --repo PATH`: train and evaluate a model on your own repo's history,
  saved with a model card to `REPO/.reviewmap/model/`.
- A pre-trained risk model ships with the package (`assets/default_model/`, with a model
  card). It runs in plain Python, so LightGBM, numpy and scikit-learn aren't needed to use it.
  Scorer fallback chain: the repo's model (`.reviewmap/model/`; for PRs and the Action, read
  from the base commit), then the shipped model, then the rules. A missing or corrupt model
  falls back with a note.
- Reading plan with the model: model levels (high = top 2%, medium = top 10% of validation
  scores) next to the rule reasons; high-risk and out-of-scope sections are read first
  whatever their size ("N more high-risk sections didn't fit the budget" when they don't);
  the rest of the budget is filled by model risk per changed line. Probabilities are never
  shown (the model isn't calibrated). `--rules-only` on `analyze` and `pr`.
- JSON (additive, still `schema_version: 1`): `items[].model_level`, `risk_scorer`,
  `summary.high_risk_unfit`.
- `review-map backtest --repo PATH | --dataset FILE`: replays held-out commits through the
  real reading plan and writes `reports/backtest.md` with examples.

### Changed
- `.env` is no longer loaded inside GitHub Actions (the working tree is the PR's code);
  LLM setup hints there point to repository secrets.
- The terminal "Safe to skim" list shows the file name for module-level code instead of
  `<module>`.
- `reading_budget.min_lines` config setting (default 40).
- `examples/demo_repo/`: a demo change whose description hides a weakened permission check.

### Fixed
- Pin `tree-sitter<0.26`: 0.26.0 segfaults when parsing larger files repeatedly.

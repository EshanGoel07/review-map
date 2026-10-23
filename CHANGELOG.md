# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

### Changed
- `.env` is no longer loaded inside GitHub Actions (the working tree is the PR's code);
  LLM setup hints there point to repository secrets.
- The terminal "Safe to skim" list shows the file name for module-level code instead of
  `<module>`.
- `reading_budget.min_lines` config setting (default 40).
- `examples/demo_repo/`: a demo change whose description hides a weakened permission check.

### Fixed
- Pin `tree-sitter<0.26`: 0.26.0 segfaults when parsing larger files repeatedly.

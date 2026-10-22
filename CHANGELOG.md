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

### Fixed
- Pin `tree-sitter<0.26`: 0.26.0 segfaults when parsing larger files repeatedly.

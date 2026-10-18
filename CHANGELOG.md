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

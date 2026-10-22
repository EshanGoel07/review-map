"""User-facing errors and exit codes.

Expected problems (bad ref, not a repo, invalid config, missing key...) raise
`ReviewMapError`. The CLI prints them as a short message plus an optional hint,
never as a traceback.
"""

# Exit codes used by the CLI.
EXIT_OK = 0
EXIT_GATE_FAILED = 1  # `--fail-on` threshold reached (Phase 6)
EXIT_USER_ERROR = 2  # bad input: unknown ref, invalid config, not a repo...


class ReviewMapError(Exception):
    """An expected error with a friendly message and an optional hint."""

    exit_code = EXIT_USER_ERROR

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class GitError(ReviewMapError):
    """A git command failed or the repository/ref is unusable."""


class ConfigError(ReviewMapError):
    """`.reviewmap.yml` is missing required structure or has invalid values."""


class LLMError(ReviewMapError):
    """The LLM is misconfigured, unreachable, or kept returning invalid output."""

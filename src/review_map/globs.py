"""Gitignore-style glob matching for repo-relative paths.

Rules (a small, predictable subset of .gitignore):
- `*` matches anything except `/`; `?` matches one character except `/`.
- `**` matches any number of directories (including none).
- A pattern without `/` matches a file or directory name at any depth:
  `*.min.js`, `yarn.lock`.
- A trailing `/` means "everything under this directory": `vendor/` matches
  `vendor/a.js` and `lib/vendor/b.js`.
- A pattern with a `/` elsewhere (or a leading `/`) is anchored at the repo root:
  `src/auth/*.py`, `/dist/`.
"""

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    pattern = pattern.strip()
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    pattern = pattern.lstrip("/")
    if pattern.endswith("/"):
        pattern += "**"
    if not anchored:
        pattern = "**/" + pattern

    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    # A match on a directory also covers everything inside it (like .gitignore).
    return re.compile("".join(out) + r"(?:/.*)?\Z")


def match_path(path: str, pattern: str) -> bool:
    """Return True if the repo-relative `path` matches the glob `pattern`."""
    return _compile(pattern).match(path) is not None


def first_match(path: str, patterns: Iterable[str]) -> str | None:
    """Return the first pattern in `patterns` that matches `path`, or None."""
    for pattern in patterns:
        if match_path(path, pattern):
            return pattern
    return None

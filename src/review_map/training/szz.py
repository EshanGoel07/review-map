"""Function-level SZZ: find the commits that introduced code a later fix had to change.

For a bug-fix commit F:
1. Take the lines F removed or changed (old side of F's diff) in non-test source files.
2. Drop lines that carry no code (blank, punctuation-only like `)`, comment-only) and
   lines the fix only re-spaced (the same code, ignoring whitespace, is added back).
3. `git blame` those lines at F's parent. Each one names the commit C that last wrote
   it, and the line number in C's version of the file.
4. The dataset (dataset.py) then labels the function in C that contains that line.

Noise filters, beyond the line filters:
- `-w`: whitespace-only changes don't count as writing a line.
- `-M`: lines moved within a file keep their original author.
- `--ignore-revs-file`: huge commits (> 50 files: reformats, vendoring) are skipped,
  so blame looks through them to the commit before.

Known limitations, kept honest:
- Fixes that only *add* lines (a missing check) blame nothing.
- Not every line a fix touches was buggy (refactors bundled with the fix).
- Keyword-detected "fixes" include some that aren't (see mine.py).
"""

import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from review_map.code.functions import SUPPORTED_LANGUAGES
from review_map.errors import GitError
from review_map.models import ChangedFile, FileStatus, LineKind
from review_map.risk.rules import is_test_path

_COMMENT_PREFIXES = ("#", "//", "/*", "*", "*/")
_PORCELAIN_HEADER = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: \d+)?$")


@dataclass(frozen=True)
class Implicated:
    """Line `line` of `path`, as written by commit `introduced`, was changed by fix `fix`."""

    introduced: str
    path: str  # the path in the introducing commit
    line: int  # line number in the introducing commit's version of the file
    fix: str

    def to_dict(self) -> dict:
        return asdict(self)


_DOC_DIRS = ("docs/", "doc/")


def is_labelable(path: str, language: str | None) -> bool:
    """Code that can be blamed for a bug: supported source files, not tests or docs
    (docs folders bundle theme and third-party scripts that aren't the product)."""
    in_docs = path.startswith(_DOC_DIRS) or any(f"/{d}" in path for d in _DOC_DIRS)
    return language in SUPPORTED_LANGUAGES and not is_test_path(path) and not in_docs


def carries_code(text: str) -> bool:
    stripped = text.strip()
    if not any(c.isalnum() for c in stripped):
        return False  # blank or punctuation-only
    return not stripped.startswith(_COMMENT_PREFIXES)


def _squash(text: str) -> str:
    return "".join(text.split())


def fixed_lines(files: list[ChangedFile]) -> dict[str, list[int]]:
    """Old-side line numbers a fix removed or changed, per old path, after the filters."""
    out: dict[str, list[int]] = {}
    for f in files:
        old_path = f.old_path or f.path
        if f.status is FileStatus.ADDED or not f.is_reviewable:
            continue
        if not is_labelable(old_path, f.language):
            continue
        lines = []
        for h in f.hunks:
            # A line the fix only re-spaced comes back among the added lines: not a fix.
            added = {_squash(ln.text) for ln in h.lines if ln.kind is LineKind.ADDED}
            lines += [
                ln.old_lineno
                for ln in h.lines
                if ln.kind is LineKind.REMOVED
                and ln.old_lineno
                and carries_code(ln.text)
                and _squash(ln.text) not in added
            ]
        if lines:
            out[old_path] = sorted(set(lines))
    return out


def _ranges(lines: list[int]) -> list[tuple[int, int]]:
    """[3, 4, 5, 9] → [(3, 5), (9, 9)], for `git blame -L`."""
    out: list[tuple[int, int]] = []
    for n in lines:
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def parse_porcelain(text: str) -> list[tuple[str, str, int]]:
    """`git blame --line-porcelain` → (commit, path in that commit, line in that commit)."""
    out = []
    sha = path = None
    line = 0
    boundary = False
    for row in text.split("\n"):
        header = _PORCELAIN_HEADER.match(row)
        if header:
            sha, line, path, boundary = header[1], int(header[2]), None, False
        elif row.startswith("filename "):
            path = row[len("filename ") :]
        elif row == "boundary":
            boundary = True  # the line comes from a root commit we can't diff
        elif row.startswith("\t") and sha and path and not boundary:
            out.append((sha, path, line))
    return out


def blame(
    repo: Path, commit: str, path: str, lines: list[int], ignore_revs: Path | None = None
) -> list[tuple[str, str, int]] | None:
    """Who last wrote `lines` of `path` as of `commit`. None if git blame failed."""
    if shutil.which("git") is None:
        raise GitError("git is not installed or not on PATH.", hint="Install git and try again.")
    args = ["git", "-C", str(repo), "blame", "--line-porcelain", "-w", "-M"]
    if ignore_revs is not None:  # absolute: git resolves it from inside `repo`
        args += ["--ignore-revs-file", str(ignore_revs.resolve())]
    for start, end in _ranges(lines):
        args += ["-L", f"{start},{end}"]
    args += [commit, "--", path]
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode != 0:
        return None  # counted by the caller, so a broken setup can't pass for clean data
    # Bytes, decoded without newline translation (a lone "\r" must not split a line).
    return parse_porcelain(result.stdout.decode("utf-8", errors="replace"))


def implicated_by_fix(
    repo: Path,
    fix_sha: str,
    parent: str,
    files: list[ChangedFile],
    ignore_revs: Path | None = None,
) -> tuple[list[Implicated], int]:
    """Every (introducing commit, path, line) the fix `fix_sha` had to change, and how
    many files git blame failed on."""
    found = []
    failures = 0
    for path, lines in fixed_lines(files).items():
        blamed = blame(repo, parent, path, lines, ignore_revs)
        if blamed is None:
            failures += 1
            continue
        for sha, src_path, line in blamed:
            found.append(Implicated(introduced=sha, path=src_path, line=line, fix=fix_sha))
    return found, failures

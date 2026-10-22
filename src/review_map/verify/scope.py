"""Compare the claimed scope with the actual diff (no LLM here, on purpose).

Every changed unit gets a `MatchLevel`:
- `function`: a function claim names it (`login` matches `UserService.login`;
  a class claim covers its methods; `auth.py:login` limits a claim to one file).
- `file` / `folder`: only its file, or a folder containing it, is mentioned.
- `none`: unclaimed.

Rules that keep hidden edits visible:
- Sibling rule: if the description names some changed functions in a file, the
  other changed functions there are unclaimed even though the file is mentioned.
- `<module>` code (imports, constants) is covered when anything else in its file
  is claimed; the risk scorer still scores it.
- Claims made only of wildcards (`**`, `.`) are ignored; broad folder claims get a note.
- Out-of-scope items are checked only when they contain a path or identifier;
  plain prose ("the login form") is listed as not checkable.
- Binary, generated and ignored files are skipped, never "unclaimed".
"""

import re
from dataclasses import dataclass

from review_map.claims.parser import extract_names, looks_like_identifier, looks_like_path
from review_map.globs import match_path
from review_map.models import (
    MODULE_UNIT,
    ChangedFile,
    ChangedUnit,
    ClaimedScope,
    ClaimSource,
    MatchLevel,
    MissingClaim,
    ScopeReport,
    ScopeViolation,
    UnitCoverage,
)

BROAD_CLAIM_FILES = 10  # a folder/glob claim covering this many files gets a note

_PATH_RANK = {MatchLevel.FILE: 2, MatchLevel.FOLDER: 1}


def normalize_path(path: str) -> str:
    path = path.strip().strip("`'\"").replace("\\", "/")
    return re.sub(r"^(?:\./)+", "", path).lstrip("/")


def is_wildcard_only(claim: str) -> bool:
    return not normalize_path(claim).strip("*/. ")


def _under(folder: str, path: str) -> bool:
    """`folder` is a whole-segment directory of `path` (`auth` in `src/auth/x.py`)."""
    return f"/{folder}/" in f"/{path}"


def path_level(claim: str, path: str) -> MatchLevel | None:
    """How a file/folder claim covers `path`: FILE, FOLDER, or None."""
    claim = normalize_path(claim)
    if not claim or is_wildcard_only(claim):
        return None
    if "*" in claim or "?" in claim:
        return MatchLevel.FOLDER if match_path(path, claim) else None
    if claim.endswith("/"):
        return MatchLevel.FOLDER if _under(claim.rstrip("/"), path) else None
    if path == claim or path.endswith("/" + claim):  # shortened paths: session.py
        return MatchLevel.FILE
    if _under(claim, path):  # a folder written without the trailing slash
        return MatchLevel.FOLDER
    return None


def _best_path_level(claims: list[str], paths: list[str]) -> tuple[MatchLevel, str] | None:
    best: tuple[MatchLevel, str] | None = None
    for claim in claims:
        for path in paths:
            level = path_level(claim, path)
            if level and (best is None or _PATH_RANK[level] > _PATH_RANK[best[0]]):
                best = (level, claim)
    return best


@dataclass(frozen=True)
class FunctionClaim:
    text: str  # as written in the description
    name: str
    file: str | None = None  # from "auth.py:login" / "auth.py::login"


def parse_function_claim(text: str) -> FunctionClaim:
    claim = text.strip().strip("`'\"")
    file = None
    if ":" in claim:
        prefix, _, rest = claim.replace("::", ":").rpartition(":")
        if looks_like_path(prefix):  # "auth.py:login"
            file, claim = prefix, rest
        else:  # "UserService::login" is C++/Rust for a method
            claim = claim.replace("::", ".")
    return FunctionClaim(text=text, name=re.sub(r"\(\)$", "", claim.strip()), file=file)


def _name_matches(claim: str, unit_name: str) -> bool:
    """`login` ~ `UserService.login`; `UserService` covers `UserService.login`."""
    return (
        unit_name == claim or unit_name.endswith("." + claim) or unit_name.startswith(claim + ".")
    )


def _module_path(path: str) -> str:
    return re.sub(r"\.[^./]+$", "", path)


def function_matches(claim: FunctionClaim, unit: ChangedUnit) -> bool:
    if unit.name == MODULE_UNIT or not claim.name:
        return False
    paths = [p for p in (unit.path, unit.old_path) if p]
    if claim.file and not any(path_level(claim.file, p) for p in paths):
        return False
    if _name_matches(claim.name, unit.name):
        return True
    # Python-style dotted paths: "auth.session.refresh_token" -> auth/session.py + refresh_token
    parts = claim.name.split(".")
    for i in range(1, len(parts)):
        module, rest = "/".join(parts[:i]), ".".join(parts[i:])
        if _name_matches(rest, unit.name) and any(
            (m := _module_path(p)) == module or m.endswith("/" + module) for p in paths
        ):
            return True
    return False


def _out_of_scope_targets(item: str) -> tuple[list[str], list[str]]:
    """Paths and names an out-of-scope item refers to; both empty = plain prose."""
    text = item.strip().strip("`'\"")
    if looks_like_path(text):
        return [text], []
    if looks_like_identifier(text):
        return [], [text]
    return extract_names(item)


def verify_scope(
    claims: ClaimedScope,
    units: list[ChangedUnit],
    files: list[ChangedFile],
    description_path: str | None = None,
) -> ScopeReport:
    """`description_path`: the repo-relative description file, which isn't itself a change."""
    report = ScopeReport(low_confidence=claims.low_confidence)
    if description_path:
        units = [u for u in units if u.path != description_path]
        files = [f for f in files if f.path != description_path]
    if claims.source is ClaimSource.HEURISTIC and (claims.files or claims.functions):
        report.notes.append("Claims came from the heuristic parser, so matches are approximate.")

    wildcards = [c for c in claims.files if is_wildcard_only(c)]
    if wildcards:
        report.notes.append(
            f"Ignored claim(s) that match everything: {', '.join(wildcards)}. "
            "A claim must name files, folders or functions."
        )
    file_claims = [c for c in claims.files if not is_wildcard_only(c)]
    function_claims = [parse_function_claim(c) for c in claims.functions]
    if not file_claims and not function_claims:
        report.notes.append(
            "The description names no files or functions, so every change counts as unclaimed."
        )

    report.skipped_files = [f.path for f in files if not f.is_reviewable]
    reviewable = [f for f in files if f.is_reviewable]
    old_path_of = {f.path: f.old_path for f in files}

    def unit_paths(u: ChangedUnit) -> list[str]:
        return [p for p in (u.path, u.old_path or old_path_of.get(u.path)) if p]

    def named_by(u: ChangedUnit) -> FunctionClaim | None:
        return next((c for c in function_claims if function_matches(c, u)), None)

    named = {u.unit_id: claim for u in units if (claim := named_by(u))}
    files_with_named = {u.path for u in units if u.unit_id in named}

    for u in units:
        coverage = UnitCoverage(unit_id=u.unit_id, path=u.path, name=u.name, level=MatchLevel.NONE)
        file_match = _best_path_level(file_claims, unit_paths(u))
        if u.unit_id in named:
            coverage.level, coverage.claim = MatchLevel.FUNCTION, named[u.unit_id].text
        elif u.name == MODULE_UNIT and file_match:
            coverage.level, coverage.claim = file_match
        elif u.name == MODULE_UNIT and u.path in files_with_named:
            coverage.level = MatchLevel.FILE
            coverage.note = "module-level code in a file with a claimed function"
        elif file_match and u.path in files_with_named:
            coverage.note = (
                f"{file_match[1]} is mentioned, but this {u.kind.value} is not "
                "(the description names other functions in this file)"
            )
        elif file_match:
            coverage.level, coverage.claim = file_match
        report.coverage.append(coverage)

    # Claims that match nothing. Skipped files still count: claiming a lockfile is fine.
    for claim in file_claims:
        if not any(path_level(claim, p) for f in files for p in (f.path, f.old_path) if p):
            report.claimed_but_missing.append(MissingClaim(kind="file", claim=claim))
    for claim in function_claims:
        if not any(function_matches(claim, u) for u in units):
            report.claimed_but_missing.append(MissingClaim(kind="function", claim=claim.text))

    for claim in file_claims:
        covered = [f for f in reviewable if path_level(claim, f.path) is MatchLevel.FOLDER]
        if len(covered) >= BROAD_CLAIM_FILES:
            report.notes.append(
                f"Broad claim {claim} covers {len(covered)} files; check they all belong."
            )

    seen: set[tuple[str, str, str | None]] = set()

    def violate(item: str, target: str, path: str, name: str | None) -> None:
        if (item, path, name) not in seen:
            seen.add((item, path, name))
            report.out_of_scope_violations.append(
                ScopeViolation(item=item, target=target, path=path, name=name)
            )

    for item in claims.out_of_scope:
        path_targets, name_targets = _out_of_scope_targets(item)
        if not path_targets and not name_targets:
            report.not_checkable.append(item)
            continue
        for target in path_targets:
            for f in reviewable:
                if not any(path_level(target, p) for p in (f.path, f.old_path) if p):
                    continue
                file_units = [u for u in units if u.path == f.path]
                for u in file_units:
                    violate(item, target, f.path, u.name)
                if not file_units:  # e.g. a pure rename: the file itself is the violation
                    violate(item, target, f.path, None)
        for target in name_targets:
            claim = parse_function_claim(target)
            for u in units:
                if function_matches(claim, u):
                    violate(item, target, u.path, u.name)

    return report

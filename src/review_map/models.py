"""Shared pydantic data models."""

from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


class FileStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class LineKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CONTEXT = "context"


class DiffLine(BaseModel):
    """One line inside a hunk.

    `old_lineno` is set for removed and context lines (position in the old file);
    `new_lineno` is set for added and context lines (position in the new file).
    """

    kind: LineKind
    text: str
    old_lineno: int | None = None
    new_lineno: int | None = None


class Hunk(BaseModel):
    """A block of nearby changes: `@@ -old_start,old_length +new_start,new_length @@`."""

    old_start: int
    old_length: int
    new_start: int
    new_length: int
    section_header: str = ""
    lines: list[DiffLine] = Field(default_factory=list)

    @property
    def added_linenos(self) -> list[int]:
        """New-file line numbers of added lines."""
        return [ln.new_lineno for ln in self.lines if ln.kind is LineKind.ADDED and ln.new_lineno]

    @property
    def removed_linenos(self) -> list[int]:
        """Old-file line numbers of removed lines."""
        return [ln.old_lineno for ln in self.lines if ln.kind is LineKind.REMOVED and ln.old_lineno]


class ChangedFile(BaseModel):
    """One file touched by the change.

    `path` is the new path (the old path for deleted files); `old_path` is set only
    for renames. Binary files have no hunks. Generated/vendored files and files
    ignored by config are kept, but flagged, so reports can mention them.
    """

    path: str
    old_path: str | None = None
    status: FileStatus
    language: str | None = None
    is_binary: bool = False
    generated_reason: str | None = None
    ignored_by: str | None = None
    added: int = 0
    removed: int = 0
    hunks: list[Hunk] = Field(default_factory=list)

    @property
    def is_generated(self) -> bool:
        return self.generated_reason is not None

    @property
    def is_reviewable(self) -> bool:
        """True if later phases should analyse this file's code."""
        return not (self.is_binary or self.is_generated or self.ignored_by)


class UnitKind(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    TYPE = "type"  # TS interface / type alias / enum
    NAMESPACE = "namespace"
    MODULE = "module"  # top-level code, or a file we cannot parse


MODULE_UNIT = "<module>"


class CodeUnit(BaseModel):
    """A function/class/method found in one version of a source file (1-based lines)."""

    name: str  # qualified, e.g. "UserService.login" or "outer.inner"
    kind: UnitKind
    start_line: int
    end_line: int


class ChangedUnit(BaseModel):
    """A code unit touched by the diff, with the changed line numbers inside it.

    `start_line`/`end_line` are in the new file (None if the unit was deleted);
    `old_start_line`/`old_end_line` are in the old file (None if the unit is new).
    For `<module>` units the range spans the changed lines only.
    """

    path: str
    old_path: str | None = None
    language: str | None = None
    name: str
    kind: UnitKind
    start_line: int | None = None
    end_line: int | None = None
    old_start_line: int | None = None
    old_end_line: int | None = None
    added_lines: list[int] = Field(default_factory=list)
    removed_lines: list[int] = Field(default_factory=list)
    added_text: list[str] = Field(default_factory=list)  # parallel to added_lines
    removed_text: list[str] = Field(default_factory=list)  # parallel to removed_lines

    @property
    def added(self) -> int:
        return len(self.added_lines)

    @property
    def removed(self) -> int:
        return len(self.removed_lines)

    @property
    def lines_changed(self) -> int:
        return self.added + self.removed

    @property
    def unit_id(self) -> str:
        return f"{self.path}::{self.name}"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskReason(BaseModel):
    rule: str
    points: int
    message: str


class RiskScore(BaseModel):
    score: int = Field(ge=0, le=100)
    level: RiskLevel
    reasons: list[RiskReason] = Field(default_factory=list)


class ScoredUnit(BaseModel):
    unit: ChangedUnit
    risk: RiskScore


class ClaimSource(StrEnum):
    LLM = "llm"
    HEURISTIC = "heuristic"


class ClaimedScope(BaseModel):
    """What a change description claims was changed (Phase 4).

    This is the author's *claim*, not the truth: the scope verifier (Phase 5)
    checks it against the actual diff.
    """

    files: list[str] = Field(default_factory=list)  # files or folders (ending in "/")
    functions: list[str] = Field(default_factory=list)  # e.g. "login", "UserService.login"
    behaviors: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    source: ClaimSource = ClaimSource.HEURISTIC
    low_confidence: bool = False
    model: str | None = None  # "provider/model" when source is llm
    notes: list[str] = Field(default_factory=list)  # warnings and hints for the user


class MatchLevel(StrEnum):
    """How directly the description claims a changed unit (Phase 5)."""

    FUNCTION = "function"  # named directly (or module-level code next to a named one)
    FILE = "file"  # only its file is mentioned
    FOLDER = "folder"  # only a folder containing it is mentioned
    NONE = "none"  # not claimed at all


class UnitCoverage(BaseModel):
    unit_id: str  # same as ChangedUnit.unit_id, to join with risk scores
    path: str
    name: str
    level: MatchLevel
    claim: str | None = None  # the claim that covers it
    note: str | None = None  # e.g. why a mentioned file still counts as unclaimed


class MissingClaim(BaseModel):
    kind: str  # "file" or "function"
    claim: str


class ScopeViolation(BaseModel):
    item: str  # the out-of-scope text from the description
    target: str  # the path or name taken from it
    path: str
    name: str | None = None  # unit name; None when the whole file is the violation


class ScopeReport(BaseModel):
    """Claimed scope vs. the actual diff. Built by plain code, never by an LLM."""

    coverage: list[UnitCoverage] = Field(default_factory=list)
    claimed_but_missing: list[MissingClaim] = Field(default_factory=list)
    out_of_scope_violations: list[ScopeViolation] = Field(default_factory=list)
    not_checkable: list[str] = Field(default_factory=list)  # out-of-scope prose, no names
    skipped_files: list[str] = Field(default_factory=list)  # binary, generated or ignored
    notes: list[str] = Field(default_factory=list)
    low_confidence: bool = False  # claims came from the heuristic parser

    @computed_field
    @property
    def unclaimed_changes(self) -> list[UnitCoverage]:
        return [c for c in self.coverage if c.level is MatchLevel.NONE]

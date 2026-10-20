"""Shared pydantic data models."""

from enum import StrEnum

from pydantic import BaseModel, Field


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

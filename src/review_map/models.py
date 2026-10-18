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

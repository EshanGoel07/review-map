"""Parse unified diffs into ChangedFile and Hunk objects."""

from pathlib import PurePosixPath

from unidiff import LINE_TYPE_ADDED, LINE_TYPE_CONTEXT, LINE_TYPE_REMOVED, PatchSet
from unidiff.errors import UnidiffParseError

from review_map.config import Config
from review_map.errors import ReviewMapError
from review_map.globs import first_match
from review_map.models import ChangedFile, DiffLine, FileStatus, Hunk, LineKind

LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".sh": "shell",
    ".sql": "sql",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
}

# (glob, reason). Matching files are still listed but not analysed as code.
GENERATED_PATTERNS: list[tuple[str, str]] = [
    *(
        (name, "lockfile")
        for name in (
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "bun.lockb",
            "poetry.lock",
            "uv.lock",
            "Pipfile.lock",
            "pdm.lock",
            "Cargo.lock",
            "Gemfile.lock",
            "composer.lock",
            "go.sum",
        )
    ),
    ("dist/", "build output"),
    ("build/", "build output"),
    ("node_modules/", "vendored"),
    ("vendor/", "vendored"),
    ("third_party/", "vendored"),
    ("*.min.js", "minified"),
    ("*.min.css", "minified"),
    ("*.js.map", "source map"),
    ("*.css.map", "source map"),
    ("*_pb2.py", "generated code"),
    ("*_pb2_grpc.py", "generated code"),
    ("*.pb.go", "generated code"),
]

_LINE_KINDS = {
    LINE_TYPE_ADDED: LineKind.ADDED,
    LINE_TYPE_REMOVED: LineKind.REMOVED,
    LINE_TYPE_CONTEXT: LineKind.CONTEXT,
}


def detect_language(path: str) -> str | None:
    return LANGUAGES.get(PurePosixPath(path).suffix.lower())


def generated_reason(path: str) -> str | None:
    for pattern, reason in GENERATED_PATTERNS:
        if first_match(path, [pattern]):
            return reason
    return None


def _strip_prefix(name: str) -> str:
    return name[2:] if name.startswith(("a/", "b/")) else name


def parse_diff(diff_text: str, config: Config | None = None) -> list[ChangedFile]:
    """Turn unified diff text into ChangedFiles, flagging binary/generated/ignored files."""
    config = config or Config()
    try:
        patch = PatchSet(diff_text)
    except UnidiffParseError as exc:
        raise ReviewMapError(f"Could not parse the diff: {exc}") from None

    files = []
    for pf in patch:
        if pf.is_added_file:
            status = FileStatus.ADDED
        elif pf.is_removed_file:
            status = FileStatus.DELETED
        elif pf.is_rename:
            status = FileStatus.RENAMED
        else:
            status = FileStatus.MODIFIED

        path = pf.path
        hunks = [
            Hunk(
                old_start=h.source_start,
                old_length=h.source_length,
                new_start=h.target_start,
                new_length=h.target_length,
                section_header=h.section_header.strip(),
                lines=[
                    DiffLine(
                        kind=_LINE_KINDS[line.line_type],
                        text=line.value.rstrip("\r\n"),
                        old_lineno=line.source_line_no,
                        new_lineno=line.target_line_no,
                    )
                    for line in h
                    if line.line_type in _LINE_KINDS  # skips "\ No newline at end of file"
                ],
            )
            for h in pf
        ]
        files.append(
            ChangedFile(
                path=path,
                old_path=_strip_prefix(pf.source_file) if status is FileStatus.RENAMED else None,
                status=status,
                language=detect_language(path),
                is_binary=pf.is_binary_file,
                generated_reason=generated_reason(path),
                ignored_by=first_match(path, config.ignore),
                added=pf.added,
                removed=pf.removed,
                hunks=[] if pf.is_binary_file else hunks,
            )
        )
    return files

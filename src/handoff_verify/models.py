"""Core data models for transcripts, claims, findings, and verification reports."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal

ClaimKind = Literal["pointer", "quote", "absence", "completeness"]
Severity = Literal["error", "warning", "info"]
SourceFormat = Literal["claude_code", "codex", "generic", "unknown"]
FileEditOperation = Literal["edit", "write", "delete", "bash_modify"]


def _serialize(value: Any) -> Any:
    """Recursively convert dataclasses, Paths, and containers to JSON-safe structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _serialize(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): _serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return str(value)


class _SerializableMixin:
    """Mixin providing deterministic JSON-safe dictionary serialization."""

    def to_dict(self) -> dict[str, Any]:
        result = _serialize(self)
        assert isinstance(result, dict)
        return result


@dataclass
class ToolEvent(_SerializableMixin):
    """Base event representing a single tool invocation and its result."""

    event_id: str = ""
    event_index: int = 0
    tool_name: str = ""
    timestamp: str | None = None
    raw_input: dict[str, Any] = field(default_factory=dict)
    raw_output: str = ""
    is_error: bool = False
    has_result: bool = True
    truncated: bool = False
    truncation_reason: str | None = None


@dataclass
class FileReadEvent(ToolEvent):
    """Structured event for a file read operation and its observed line window."""

    file_path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    offset: int | None = None
    limit: int | None = None
    content: str = ""
    line_count: int = 0


@dataclass
class FileEditEvent(ToolEvent):
    """Structured event for a file modification, creation, or deletion."""

    file_path: str = ""
    operation: FileEditOperation | str = "edit"
    old_text: str | None = None
    new_text: str | None = None
    content: str | None = None


@dataclass
class SearchEvent(ToolEvent):
    """Structured event for a repository search or directory listing."""

    query: str = ""
    search_path: str | None = None
    search_paths: list[str] = field(default_factory=list)
    glob_pattern: str | None = None
    excluded_dirs: list[str] = field(default_factory=list)
    match_count: int | None = None
    matched_files: list[str] = field(default_factory=list)
    output: str = ""
    result_limit: int | None = None


@dataclass
class CommandEvent(ToolEvent):
    """Structured event for a shell command execution."""

    command: str = ""
    cwd: str | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


@dataclass
class PaginationEvent(ToolEvent):
    """Structured event capturing paginated tool responses and cursor follow-through."""

    resource: str = ""
    page: int | None = None
    total_pages: int | None = None
    has_more: bool = False
    next_cursor: str | None = None
    cursor_followed: bool = False
    item_count: int | None = None


@dataclass
class AssistantMessage(_SerializableMixin):
    """Text emitted by the assistant in a transcript."""

    index: int = 0
    text: str = ""
    timestamp: str | None = None
    is_final: bool = False
    after_event_index: int | None = None


@dataclass
class ParsedTranscript(_SerializableMixin):
    """Unified normalized representation of a parsed agent transcript.

    Invariant:
        - `events` contains every observed tool call in chronological order, including
          unresolved (`has_result=False`) and errored (`is_error=True`) calls.
        - `file_reads`, `file_edits`, `searches`, and `paginations` contain only completed,
          non-errored evidence events (so downstream verifiers never treat failed or
          unreturned calls as observed evidence).
        - `commands` contains completed shell command executions (`has_result=True`),
          preserving non-zero `exit_code` and `is_error` for command-output verification.
    """

    source_format: str = "unknown"
    transcript_path: str | None = None
    cwd: str | None = None
    events: list[ToolEvent] = field(default_factory=list)
    file_reads: list[FileReadEvent] = field(default_factory=list)
    file_edits: list[FileEditEvent] = field(default_factory=list)
    searches: list[SearchEvent] = field(default_factory=list)
    commands: list[CommandEvent] = field(default_factory=list)
    paginations: list[PaginationEvent] = field(default_factory=list)
    assistant_messages: list[AssistantMessage] = field(default_factory=list)
    final_text: str = ""
    malformed_lines: int = 0
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class Claim(_SerializableMixin):
    """A verifiable claim extracted from an assistant message."""

    kind: ClaimKind
    text: str
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    snippet: str | None = None
    context: str = ""
    message_index: int | None = None


@dataclass
class Finding(_SerializableMixin):
    """A verification finding raised against a claim or transcript scope."""

    severity: Severity
    code: str
    claim: Claim | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    suggestion: str | None = None


@dataclass
class ScopeReceipt(_SerializableMixin):
    """Factual summary of what the agent actually read, searched, edited, or left unread."""

    searched_roots: list[str] = field(default_factory=list)
    excluded_dirs: list[str] = field(default_factory=list)
    truncated_outputs: list[dict[str, Any]] = field(default_factory=list)
    unread_cursors: list[dict[str, Any]] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    files_edited: list[str] = field(default_factory=list)


@dataclass
class VerificationReport(_SerializableMixin):
    """Top-level verification output returned by verify_transcript()."""

    schema_version: str = "1.0"
    transcript_path: str | None = None
    source_format: str = "unknown"
    claims: list[Claim] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    scope_receipt: ScopeReceipt = field(default_factory=ScopeReceipt)
    passed: bool = True
    duration_ms: float = 0.0

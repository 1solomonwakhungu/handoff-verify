"""Transcript parser registry, format auto-detection, and unified entry points."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from handoff_verify.models import ParsedTranscript, SourceFormat
from handoff_verify.parsers.claude_code import parse_claude_code
from handoff_verify.parsers.codex import parse_codex
from handoff_verify.parsers.generic import parse_generic

_CLAUDE_TOP_TYPES = frozenset(
    {
        "assistant",
        "user",
        "system",
        "progress",
        "summary",
        "file-history-snapshot",
        "queue-operation",
    }
)
_CODEX_MODERN_TYPES = frozenset({"response_item", "session_meta", "event_msg", "turn_context"})
_CODEX_LEGACY_TYPES = frozenset(
    {
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "local_shell_call",
        "local_shell_call_output",
    }
)
_GENERIC_RECORD_TYPES = frozenset(
    {
        "meta",
        "file_read",
        "file_edit",
        "search",
        "command",
        "pagination",
        "tool_call",
        "tool_result",
    }
)


def _iter_sample_lines(source: str | Path | Iterable[str], max_valid: int = 200) -> list[dict]:
    """Read up to `max_valid` valid JSON object lines from a path or line iterable."""
    records: list[dict] = []

    def _consume(lines: Iterable[str]) -> None:
        for idx, raw in enumerate(lines):
            if idx == 0 and raw.startswith("\ufeff"):
                raw = raw.lstrip("\ufeff")
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except (ValueError, RecursionError):
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
                if len(records) >= max_valid:
                    break

    if isinstance(source, (str, Path)):
        with Path(source).open("r", encoding="utf-8-sig", errors="replace") as fh:
            _consume(fh)
    else:
        _consume(source)
    return records


def detect_format(path_or_lines: str | Path | Iterable[str]) -> SourceFormat:
    """Detect whether a JSONL transcript is `claude_code`, `codex`, `generic`, or `unknown`."""
    records = _iter_sample_lines(path_or_lines)
    if not records:
        return "unknown"

    scores: dict[str, int] = {"claude_code": 0, "codex": 0, "generic": 0}

    for rec in records:
        rec_type = rec.get("type")
        msg = rec.get("message")
        payload = rec.get("payload")

        # Strong Codex signals
        if rec_type in _CODEX_MODERN_TYPES and isinstance(payload, dict):
            scores["codex"] += 4
            continue
        if rec_type in _CODEX_LEGACY_TYPES:
            scores["codex"] += 4
            continue
        if rec_type == "message" and rec.get("role") in ("assistant", "user"):
            content = rec.get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") in ("output_text", "input_text")
                for b in content
            ):
                scores["codex"] += 4
                continue

        # Strong Claude Code signals
        if rec_type in _CLAUDE_TOP_TYPES and isinstance(msg, dict):
            scores["claude_code"] += 3
            content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result", "thinking")
                for b in content
            ):
                scores["claude_code"] += 3
            continue
        if (
            rec_type == "summary" and isinstance(rec.get("summary"), str) and "leafUuid" in rec
        ) or (rec_type == "file-history-snapshot" and "snapshot" in rec):
            scores["claude_code"] += 3
            continue
        if any(k in rec for k in ("sessionId", "parentUuid", "toolUseResult", "isSidechain")):
            scores["claude_code"] += 3
            continue

        # Strong Generic signals
        if rec_type in _GENERIC_RECORD_TYPES:
            scores["generic"] += 4
            continue
        if (
            rec_type == "assistant"
            and not isinstance(msg, dict)
            and (isinstance(rec.get("text"), str) or isinstance(rec.get("content"), str))
        ):
            scores["generic"] += 3
            continue

    best_format = max(scores, key=lambda k: scores[k])
    if scores[best_format] == 0:
        return "unknown"
    return best_format  # type: ignore[return-value]


def auto_parse(
    path: str | Path | Iterable[str],
    *,
    format_hint: str | None = None,
    transcript_path: str | None = None,
) -> ParsedTranscript:
    """Auto-detect transcript format (unless `format_hint` is given) and parse it."""
    # Materialize non-reentrant iterables once so detection + parsing both see all lines
    source: str | Path | list[str] = path if isinstance(path, (str, Path)) else list(path)
    fmt = format_hint if format_hint and format_hint != "auto" else detect_format(source)

    if fmt == "claude_code":
        return parse_claude_code(source, transcript_path=transcript_path)
    if fmt == "codex":
        return parse_codex(source, transcript_path=transcript_path)
    if fmt == "generic":
        return parse_generic(source, transcript_path=transcript_path, source_format="generic")
    if fmt == "unknown":
        return parse_generic(source, transcript_path=transcript_path, source_format="unknown")
    raise ValueError(
        f"Unsupported transcript format {fmt!r}; expected 'auto', 'claude_code', 'codex', "
        f"or 'generic'."
    )


__all__ = [
    "auto_parse",
    "detect_format",
    "parse_claude_code",
    "parse_codex",
    "parse_generic",
]

"""Shared JSONL loading, line-range, truncation, pagination, and shell helpers."""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from handoff_verify.models import (
    FileEditEvent,
    FileReadEvent,
    PaginationEvent,
    SearchEvent,
    ToolEvent,
)

MAX_PARSE_WARNINGS = 50
_MAX_JSON_PARSE_BYTES = 10_000_000  # 10 MB guard for inline tool output JSON


@dataclass
class JsonlLoadResult:
    """Result of reading a JSONL source with malformed-line resilience."""

    records: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    malformed_lines: int = 0
    parse_warnings: list[str] = field(default_factory=list)


def load_jsonl_lines(lines: Iterable[str]) -> JsonlLoadResult:
    """Parse JSONL lines into dict records, never crashing on malformed or pathological lines."""
    result = JsonlLoadResult()
    suppressed = 0
    for line_no, raw_line in enumerate(lines, start=1):
        if line_no == 1 and raw_line.startswith("\ufeff"):
            raw_line = raw_line.lstrip("\ufeff")
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except (ValueError, RecursionError) as exc:
            result.malformed_lines += 1
            if len(result.parse_warnings) < MAX_PARSE_WARNINGS:
                msg = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
                result.parse_warnings.append(f"Line {line_no}: invalid JSON ({msg})")
            else:
                suppressed += 1
            continue
        if not isinstance(parsed, dict):
            result.malformed_lines += 1
            if len(result.parse_warnings) < MAX_PARSE_WARNINGS:
                result.parse_warnings.append(
                    f"Line {line_no}: expected JSON object, got {type(parsed).__name__}"
                )
            else:
                suppressed += 1
            continue
        result.records.append((line_no, parsed))
    if suppressed:
        result.parse_warnings.append(f"... and {suppressed} additional malformed-line warnings")
    return result


def load_jsonl(source: str | Path | Iterable[str]) -> tuple[JsonlLoadResult, str | None]:
    """Load JSONL from a file path or an iterable of lines."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
            return load_jsonl_lines(fh), str(path)
    return load_jsonl_lines(source), None


def safe_int(value: Any) -> int | None:
    """Convert an int-like value to int, rejecting booleans and oversized digit strings."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s and len(s) <= 100 and (s.isdigit() or (s.startswith("-") and s[1:].isdigit())):
            try:
                return int(s)
            except ValueError:
                return None
    return None


def safe_str(value: Any) -> str:
    """Coerce optional values to string cleanly."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def extract_text_content(content: Any) -> str:
    """Extract text from string or list-of-blocks tool_result / message content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                block_type = item.get("type")
                if block_type in ("text", "output_text", "input_text", None) and isinstance(
                    item.get("text"), str
                ):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str) and block_type != "thinking":
                    parts.append(item["content"])
        return "\n".join(parts)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("output"), str):
            return content["output"]
        try:
            return json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError, RecursionError):
            return str(content)
    return str(content)


def split_lf_lines(text: str) -> list[str]:
    """Split text strictly on CRLF/LF (preserving form feed \\x0c and U+2028 inside lines)."""
    if not text:
        return []
    normalized = text.replace("\r\n", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    return normalized.split("\n")


# --- Line-numbered read output parsing ---

_ARROW_LINE_RE = re.compile(r"^\s*(\d+)→(.*)$")
_TAB_LINE_RE = re.compile(r"^(\s*)(\d+)\t(.*)$")
_TRAILING_NOTICE_RE = re.compile(
    r"^(?:\.\.\.|\[.*truncated.*\]|Note:|Showing\s+|File\s+content\s+truncated|"
    r"Results?\s+(?:are|were)\s+truncated|"
    r"\(\d+\s+more\s+lines?\)|\(End of file)",
    re.IGNORECASE,
)


def _strip_trailing_reminders(raw_output: str) -> tuple[str, list[str]]:
    """Strip only trailing line-start <system-reminder> blocks, keeping in-file tags intact."""
    reminders: list[str] = []
    text = raw_output
    open_tag = "<system-reminder>"
    close_tag = "</system-reminder>"
    while True:
        stripped_end = text.rstrip()
        if not stripped_end.endswith(close_tag):
            break
        open_idx = stripped_end.rfind(open_tag)
        if open_idx == -1:
            break
        if open_idx > 0 and stripped_end[open_idx - 1] != "\n":
            break
        reminders.append(stripped_end[open_idx + len(open_tag) : -len(close_tag)])
        text = stripped_end[:open_idx].rstrip("\r\n")
    return text, reminders


@dataclass
class ParsedReadOutput:
    """Parsed content and line window extracted from a Read tool result."""

    content: str
    start_line: int | None
    end_line: int | None
    line_count: int
    truncated: bool = False
    truncation_reason: str | None = None


def parse_numbered_lines(
    raw_output: str,
    *,
    offset: int | None = None,
    limit: int | None = None,
    expect_line_numbers: bool = True,
) -> ParsedReadOutput:
    """Parse Claude Code / cat -n line-numbered output into clean content and line bounds."""
    cleaned_raw, trailing_reminders = _strip_trailing_reminders(raw_output)
    if not cleaned_raw:
        return ParsedReadOutput(content="", start_line=None, end_line=None, line_count=0)

    lines = split_lf_lines(cleaned_raw)
    if not lines:
        return ParsedReadOutput(content="", start_line=None, end_line=None, line_count=0)

    truncated = False
    truncation_reason: str | None = None

    # Inspect trailing non-code notice lines (e.g., "... (50 more lines)" or "[output truncated]")
    effective_lines = list(lines)
    while effective_lines and _TRAILING_NOTICE_RE.search(effective_lines[-1].strip()):
        notice = effective_lines[-1].strip()
        notice_trunc, notice_reason = detect_truncation(notice, tool_name="Read")
        if notice_trunc:
            truncated = True
            truncation_reason = notice_reason
            effective_lines.pop()
        elif notice.lower().startswith("(end of file"):
            effective_lines.pop()
        else:
            break

    if not truncated and trailing_reminders:
        for reminder in trailing_reminders:
            r_trunc, r_reason = detect_truncation(reminder, tool_name="Read")
            if r_trunc:
                truncated = True
                truncation_reason = r_reason
                break

    if expect_line_numbers and effective_lines:
        # 1. Try Claude Code's `   12→code` format (strictly increasing line numbers)
        arrow_nums: list[int] = []
        arrow_text: list[str] = []
        arrow_ok = True
        prev_num = 0
        for line in effective_lines:
            m = _ARROW_LINE_RE.match(line)
            if not m:
                arrow_ok = False
                break
            num = int(m.group(1))
            if num <= prev_num:
                arrow_ok = False
                break
            prev_num = num
            arrow_nums.append(num)
            arrow_text.append(m.group(2))
        if arrow_ok and arrow_nums:
            return ParsedReadOutput(
                content="\n".join(arrow_text),
                start_line=arrow_nums[0],
                end_line=arrow_nums[-1],
                line_count=len(arrow_nums),
                truncated=truncated,
                truncation_reason=truncation_reason,
            )

        # 2. Try `cat -n` / `nl` / `read_file` tab format: require consecutive increments
        #    and either starting at 1, matching an explicit offset, or having leading padding
        #    spaces (so unpadded TSV like `100\tAlice\n101\tBob` is not misparsed).
        tab_nums: list[int] = []
        tab_text: list[str] = []
        tab_ok = True
        has_padding = False
        prev_num = 0
        for idx, line in enumerate(effective_lines):
            m = _TAB_LINE_RE.match(line)
            if not m:
                tab_ok = False
                break
            if m.group(1):
                has_padding = True
            num = int(m.group(2))
            if idx == 0:
                if num < 1:
                    tab_ok = False
                    break
            elif num != prev_num + 1:
                tab_ok = False
                break
            prev_num = num
            tab_nums.append(num)
            tab_text.append(m.group(3))
        if (
            tab_ok
            and tab_nums
            and (tab_nums[0] == 1 or (offset is not None and tab_nums[0] == offset) or has_padding)
        ):
            return ParsedReadOutput(
                content="\n".join(tab_text),
                start_line=tab_nums[0],
                end_line=tab_nums[-1],
                line_count=len(tab_nums),
                truncated=truncated,
                truncation_reason=truncation_reason,
            )

    # Unnumbered output: check for mid-output elision markers before inferring a contiguous window
    line_count = len(effective_lines)
    if line_count == 0:
        return ParsedReadOutput(
            content="",
            start_line=None,
            end_line=None,
            line_count=0,
            truncated=truncated,
            truncation_reason=truncation_reason,
        )
    joined_content = "\n".join(effective_lines)
    mid_elided, mid_reason = _scan_lines_for_truncation_banner(joined_content)
    if mid_elided:
        return ParsedReadOutput(
            content=joined_content,
            start_line=None,
            end_line=None,
            line_count=line_count,
            truncated=True,
            truncation_reason=truncation_reason or mid_reason,
        )
    start_line = offset if (offset is not None and offset >= 1) else 1
    end_line = start_line + line_count - 1
    return ParsedReadOutput(
        content=joined_content,
        start_line=start_line,
        end_line=end_line,
        line_count=line_count,
        truncated=truncated,
        truncation_reason=truncation_reason,
    )


# --- Truncation detection (linear time, anchored to head/tail banners) ---

_GREP_MATCH_LINE_RE = re.compile(r"^[^\s:][^:]*:\d+:")
_SHOWING_FIRST_RE = re.compile(
    r"^(?:\[|\(|note:\s*)?showing\s+(?:the\s+)?first\s+(\d+)\s+(?:of|out\s+of)\s+(\d+)\b",
    re.IGNORECASE,
)
_SHOWING_RANGE_RE = re.compile(
    r"^(?:\[|\(|note:\s*)?showing\s+(?:lines?|results?|items?)\s+(\d+)\s*[-–]\s*(\d+)\s+of\s+(\d+)\b",
    re.IGNORECASE,
)
_INLINE_CODEX_ELISION_RE = re.compile(
    r"(?:…|\.\.\.)\s*\d+\s+(?:tokens?|chars?|characters?|lines?)\s+truncated\s*(?:…|\.\.\.)",
    re.IGNORECASE,
)
_BANNER_TRUNCATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"^(?:\.\.\.\s*)?\[[^\[\]\n]{0,160}\b(?:truncated|omitted)\b[^\[\]\n]{0,160}\](?:\s*\.\.\.)?$",
            re.IGNORECASE,
        ),
        "truncated",
    ),
    (
        re.compile(
            r"^\([^()\n]{0,160}(?:\btruncated\b|\d+\s+[^()\n]{0,40}\bomitted\b|"
            r"\.\.\.[^()\n]{0,80}\bomitted\b|"
            r"\b(?:output|rest|remaining|content|lines?)\s+omitted\b)[^()\n]{0,160}\)\.?$",
            re.IGNORECASE,
        ),
        "truncated",
    ),
    (
        re.compile(
            r"^(?:\.\.\.\s*)?(?:output|results?|response|file\s+content)\s+"
            r"(?:was|were|is|are)?\s*truncated\b",
            re.IGNORECASE,
        ),
        "output truncated",
    ),
    (
        re.compile(
            r"^(?:\.\.\.\s*)?truncated\s+(?:(?:to|after|at)\s+\d+|\d+\s+(?:lines?|chars?|characters?|bytes?|matches?|results?))\b",
            re.IGNORECASE,
        ),
        "output truncated",
    ),
    (
        re.compile(
            r"^(?:\.\.\.\s*)?(?:output|response|content|file)\s+"
            r"(?:exceeded\s+(?:the\s+)?(?:max|maximum|limit|size|budget|token\s+budget|\d+)|"
            r"is\s+longer\s+than\s+(?:the\s+)?(?:max|maximum|\d+))\b",
            re.IGNORECASE,
        ),
        "output limit exceeded",
    ),
    (
        re.compile(
            r"^(?:\[|\(|note:\s*)?(?:head_limit|result\s+limit|output\s+limit)\b.*\breached\b",
            re.IGNORECASE,
        ),
        "limit reached",
    ),
    (
        re.compile(r"^(?:\[|\()?\s*limit\s+reached\s*(?:\]|\))?\.?$", re.IGNORECASE),
        "limit reached",
    ),
    (
        re.compile(
            r"^(?:\.\.\.\s*\(?|\()\s*\d+\s+more\s+"
            r"(?:lines?|matches?|results?|files?|chars?|characters?|bytes?)\s*\)?\.?$",
            re.IGNORECASE,
        ),
        "additional output omitted",
    ),
    (
        re.compile(
            r"^\(\s*\d+\s+(?:lines?|matches?|results?|chars?|characters?)\s+"
            r"(?:omitted|truncated|skipped)\s*\)\.?$",
            re.IGNORECASE,
        ),
        "output omitted",
    ),
)

# Fast linear keyword probe used to avoid per-line regex checks on outputs with no banners
_TRUNCATION_KEYWORD_RE = re.compile(
    r"truncat|omitted|limit\s+reached|more\s+(?:lines?|matches?|results?|files?|chars?|characters?|bytes?)"
    r"|showing\s+(?:the\s+)?first|showing\s+(?:lines?|results?|items?)|exceeded|longer\s+than",
    re.IGNORECASE,
)


def is_truncation_banner_line(line: str) -> tuple[bool, str | None]:
    """Return (True, reason) if a single standalone line is an explicit truncation banner."""
    s = line.strip()
    if not s:
        return False, None
    m_inline = _INLINE_CODEX_ELISION_RE.search(s)
    if m_inline:
        return True, m_inline.group(0).strip()
    if len(s) > 240:
        return False, None
    # Never treat a `path/to/file.py:42:code` grep match line as a truncation banner
    if _GREP_MATCH_LINE_RE.match(s):
        return False, None

    m_first = _SHOWING_FIRST_RE.match(s)
    if m_first:
        shown, total = int(m_first.group(1)), int(m_first.group(2))
        if shown < total:
            return True, f"showing first {shown} of {total}"
        return False, None

    m_range = _SHOWING_RANGE_RE.match(s)
    if m_range:
        end_idx, total = int(m_range.group(2)), int(m_range.group(3))
        if end_idx < total:
            return True, f"showing up to {end_idx} of {total}"
        return False, None

    for pattern, reason in _BANNER_TRUNCATION_PATTERNS:
        m = pattern.match(s)
        if m:
            snippet = s if len(s) <= 80 else reason
            return True, snippet
    return False, None


def _try_parse_json_object(text: str) -> dict[str, Any] | None:
    if not text or len(text) > _MAX_JSON_PARSE_BYTES:
        return None
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return None
    try:
        data = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _sample_head_tail_lines(text: str, *, head: int = 5, tail: int = 10) -> list[str]:
    """Extract a bounded slice of head and tail lines in O(1) time even for huge outputs."""
    if not text:
        return []
    head_chunk = text[:2048]
    tail_chunk = text[-4096:] if len(text) > 4096 else text
    head_lines = split_lf_lines(head_chunk)[:head]
    tail_lines = split_lf_lines(tail_chunk)[-tail:]
    if len(text) <= 4096:
        all_lines = split_lf_lines(text)
        if len(all_lines) <= head + tail:
            return all_lines
    return head_lines + tail_lines


def _scan_lines_for_truncation_banner(text: str) -> tuple[bool, str | None]:
    """Linear O(n) scan that catches head, mid-output elision, and tail truncation banners."""
    pos = 0
    n = len(text)
    while pos < n:
        m = _TRUNCATION_KEYWORD_RE.search(text, pos)
        if not m:
            return False, None
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end == -1:
            line_end = n
        candidate = text[line_start:line_end].rstrip("\r")
        is_trunc, reason = is_truncation_banner_line(candidate)
        if is_trunc:
            return True, reason
        pos = line_end + 1
    return False, None


def detect_truncation(
    text: str,
    *,
    tool_name: str = "",
    result_limit: int | None = None,
    observed_count: int | None = None,
) -> tuple[bool, str | None]:
    """Detect explicit truncation/elision markers in linear time without content false positives."""
    _ = tool_name
    if (
        result_limit is not None
        and result_limit > 0
        and observed_count is not None
        and observed_count >= result_limit
    ):
        return True, f"result limit ({result_limit}) reached"

    if not text:
        return False, None

    # Check structured JSON flags first
    obj = _try_parse_json_object(text)
    if obj is not None:
        for container in _iter_metadata_dicts(obj):
            for flag in (
                "truncated",
                "isTruncated",
                "is_truncated",
                "limit_reached",
                "limitReached",
            ):
                if container.get(flag) is True:
                    return True, f"JSON flag {flag}=true"
            msg = container.get("truncation_reason") or container.get("warning")
            if isinstance(msg, str) and "truncat" in msg.lower():
                return True, msg.strip()
        return False, None

    return _scan_lines_for_truncation_banner(text)


# --- Pagination detection and cursor tracking ---

_TEXT_PAGE_OF_RE = re.compile(
    r"^[\s*-]*(?:showing\s+)?page\s+(\d+)\s+of\s+(\d+)\b",
    re.IGNORECASE,
)
_TEXT_CURSOR_LINE_RE = re.compile(
    r"^[\s*-]*(?:next[_ ]?cursor|next[_ ]?page[_ ]?token|continuation[_ ]?token)\s*:\s*"
    r"['\"]?([A-Za-z0-9._:/+=-]{2,128})['\"]?\s*$",
    re.IGNORECASE,
)
_TEXT_HAS_MORE_LINE_RE = re.compile(
    r"^[\s*-]*(?:has_more|hasMore|hasNextPage)\s*:\s*true\s*$",
    re.IGNORECASE,
)
_INVALID_CURSOR_TOKENS = frozenset(
    {
        "null",
        "none",
        "false",
        "true",
        "empty",
        "undefined",
        "response",
        "result",
        "data",
        "body",
        "payload",
        "cursor",
        "next_cursor",
    }
)


def _iter_metadata_dicts(
    obj: dict[str, Any], *, max_depth: int = 6, max_nodes: int = 64
) -> list[dict[str, Any]]:
    """Return nested dict containers via bounded BFS (covers GraphQL data.*.pageInfo)."""
    containers: list[dict[str, Any]] = []
    queue: list[tuple[dict[str, Any], int]] = [(obj, 0)]
    idx = 0
    while idx < len(queue) and len(containers) < max_nodes:
        current, depth = queue[idx]
        idx += 1
        containers.append(current)
        if depth >= max_depth:
            continue
        for val in current.values():
            if isinstance(val, dict):
                queue.append((val, depth + 1))
    return containers


def _infer_resource_name(tool_name: str, raw_input: dict[str, Any]) -> str:
    for key in (
        "resource",
        "repo",
        "repository",
        "project",
        "owner",
        "table",
        "collection",
        "url",
        "path",
        "query",
    ):
        val = raw_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return tool_name


def detect_pagination(
    *,
    event_id: str,
    event_index: int,
    tool_name: str,
    timestamp: str | None,
    raw_input: dict[str, Any],
    raw_output: str,
    is_error: bool = False,
) -> PaginationEvent | None:
    """Extract a PaginationEvent from structured tool outputs without false positives on code."""
    if is_error or not raw_output:
        return None

    page: int | None = None
    total_pages: int | None = None
    has_more = False
    next_cursor: str | None = None
    item_count: int | None = None
    found_signal = False

    obj = _try_parse_json_object(raw_output)
    if obj is not None:
        explicit_has_more: bool | None = None
        for container in _iter_metadata_dicts(obj):
            if next_cursor is None:
                for cursor_key in (
                    "nextCursor",
                    "next_cursor",
                    "nextPageToken",
                    "next_page_token",
                    "continuationToken",
                    "continuation_token",
                    "endCursor",
                ):
                    val = container.get(cursor_key)
                    if isinstance(val, str) and val.strip():
                        next_cursor = val.strip()
                        found_signal = True
                        break

            for more_key in (
                "has_more",
                "hasMore",
                "hasNextPage",
                "has_next_page",
                "more_available",
            ):
                val = container.get(more_key)
                if isinstance(val, bool):
                    explicit_has_more = val
                    if val:
                        has_more = True
                    found_signal = True

            if page is None:
                for p_key in ("page", "current_page", "currentPage"):
                    p_val = safe_int(container.get(p_key))
                    if p_val is not None:
                        page = p_val
                        break
            if total_pages is None:
                for tp_key in ("total_pages", "totalPages", "page_count", "pageCount"):
                    tp_val = safe_int(container.get(tp_key))
                    if tp_val is not None:
                        total_pages = tp_val
                        break

            if item_count is None:
                for list_key in ("items", "results", "data", "nodes", "edges", "entries", "rows"):
                    val = container.get(list_key)
                    if isinstance(val, list):
                        item_count = len(val)
                        break

        if page is not None and total_pages is not None:
            found_signal = True
            if page < total_pages and explicit_has_more is not False:
                has_more = True
        if next_cursor is not None and explicit_has_more is None:
            has_more = True
        if explicit_has_more is False:
            has_more = False
    else:
        # Line-anchored text pagination detection on bounded head/tail lines only
        for line in _sample_head_tail_lines(raw_output, head=8, tail=12):
            s = line.strip()
            if not s or _GREP_MATCH_LINE_RE.match(s):
                continue
            m_page = _TEXT_PAGE_OF_RE.match(s)
            if m_page and page is None:
                page = int(m_page.group(1))
                total_pages = int(m_page.group(2))
                found_signal = True
                if page < total_pages:
                    has_more = True
                continue

            m_cur = _TEXT_CURSOR_LINE_RE.match(s)
            if m_cur and next_cursor is None:
                candidate = m_cur.group(1).strip()
                if candidate.lower() not in _INVALID_CURSOR_TOKENS:
                    next_cursor = candidate
                    has_more = True
                    found_signal = True
                continue

            if _TEXT_HAS_MORE_LINE_RE.match(s):
                has_more = True
                found_signal = True

    if not found_signal:
        return None

    return PaginationEvent(
        event_id=event_id,
        event_index=event_index,
        tool_name=tool_name,
        timestamp=timestamp,
        raw_input=raw_input,
        raw_output=raw_output,
        is_error=is_error,
        has_result=True,
        resource=_infer_resource_name(tool_name, raw_input),
        page=page,
        total_pages=total_pages,
        has_more=has_more,
        next_cursor=next_cursor,
        cursor_followed=False,
        item_count=item_count,
    )


def _contains_exact_string_value(data: Any, needle: str) -> bool:
    """Recursively check whether `needle` equals any string value in `data` (exact match)."""
    if isinstance(data, str):
        return data == needle
    if isinstance(data, dict):
        return any(_contains_exact_string_value(v, needle) for v in data.values())
    if isinstance(data, (list, tuple)):
        return any(_contains_exact_string_value(item, needle) for item in data)
    return False


def _extract_offset_param(raw_input: dict[str, Any]) -> int | None:
    for key in ("offset", "skip", "start", "startIndex", "start_index"):
        val = safe_int(raw_input.get(key))
        if val is not None:
            return val
    return None


def mark_followed_cursors(
    events: list[ToolEvent],
    paginations: list[PaginationEvent],
) -> None:
    """Mark each PaginationEvent as cursor_followed if a later same-tool call advances it."""
    for p in paginations:
        if p.cursor_followed or not p.has_more:
            continue
        prev_offset = _extract_offset_param(p.raw_input) or 0

        for later in events:
            if later.event_index <= p.event_index or not later.has_result or later.is_error:
                continue
            if later.tool_name != p.tool_name:
                continue

            # 1. Exact cursor match on same tool
            if p.next_cursor and _contains_exact_string_value(later.raw_input, p.next_cursor):
                p.cursor_followed = True
                break

            later_resource = _infer_resource_name(later.tool_name, later.raw_input)
            same_resource = (not p.resource) or (later_resource == p.resource)

            # 2. Page-number advancement on same tool and resource
            if (
                same_resource
                and p.page is not None
                and p.total_pages is not None
                and p.page < p.total_pages
            ):
                later_page = safe_int(
                    later.raw_input.get("page")
                    or later.raw_input.get("current_page")
                    or later.raw_input.get("currentPage")
                )
                if later_page is not None and later_page > p.page:
                    p.cursor_followed = True
                    break

            # 3. Offset/skip advancement on same tool and resource
            if same_resource and p.next_cursor is None and p.page is None:
                later_offset = _extract_offset_param(later.raw_input)
                if later_offset is not None and later_offset > prev_offset:
                    p.cursor_followed = True
                    break


# --- Shell command analysis ---

_HEREDOC_OP_RE = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_EXIT_CODE_LINE_RE = re.compile(r"^\s*Exit code[:\s]+(-?\d+)\s*$", re.IGNORECASE)
_NO_MATCH_OUTPUT_SENTINELS = frozenset(
    {
        "no matches found",
        "no files found",
        "no matching files found",
        "0 matches found",
    }
)
_PASSTHROUGH_PIPE_CMDS = frozenset({"head", "tail", "sort", "uniq", "cat", "tee"})
_COMMON_SEARCH_VALUE_FLAGS = frozenset(
    {
        "-A",
        "--after-context",
        "-B",
        "--before-context",
        "-C",
        "--context",
        "--color",
        "-d",
        "-D",
        "--devices",
        "--directories",
        "--binary-files",
        "-f",
        "--file",
        "--label",
        "--exclude-from",
        "--max-depth",
    }
)
_RG_ONLY_VALUE_FLAGS = frozenset(
    {
        "--context-separator",
        "--colors",
        "-E",
        "--engine",
        "--encoding",
        "--field-match-separator",
        "-j",
        "--threads",
        "-M",
        "--max-columns",
        "--max-filesize",
        "--path-separator",
        "--pre",
        "--pre-glob",
        "-r",
        "--replace",
        "--sort",
        "--sortr",
        "-t",
        "--type",
        "-T",
        "--type-not",
    }
)
_AG_ACK_VALUE_FLAGS = frozenset(
    {
        "-G",
        "--file-search-regex",
        "--depth",
        "--ignore-dir",
        "--type",
        "-t",
    }
)
_PATCH_HEADER_RE = re.compile(
    r"^\*\*\*\s+(Update File|Add File|Delete File|Move to):\s*(.+?)\s*$",
    re.MULTILINE,
)
_SED_RANGE_RE = re.compile(r"^(\d+)(?:,(\d+|\$))?p$")


def unwrap_shell_command(command: str | list[str]) -> str:
    """Normalize a string or argv list (including bash -lc '...') to a shell command string."""
    if isinstance(command, str):
        return command.strip()
    if isinstance(command, list):
        parts = [str(x) for x in command]
        if (
            len(parts) >= 3
            and Path(parts[0]).name in ("bash", "sh", "zsh")
            and parts[1] in ("-c", "-lc", "-cl")
        ):
            return parts[2].strip()
        return shlex.join(parts)
    return safe_str(command).strip()


def _collapse_line_continuations(command: str) -> str:
    """Collapse unquoted backslash-newline continuations (`\\\\\\n`) in shell commands."""
    if "\\\n" not in command and "\\\r\n" not in command:
        return command
    normalized = command.replace("\r\n", "\n")
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(normalized)
    while i < n:
        ch = normalized[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n and normalized[i + 1] == "\n":
            # Line continuation outside single quotes (both unquoted and inside double quotes)
            i += 2
            continue
        if quote == '"':
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(normalized[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(normalized[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_heredocs(command: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove heredoc bodies and collapse line continuations, returning (clean_cmd, heredocs)."""
    collapsed = _collapse_line_continuations(command)
    lines = split_lf_lines(collapsed)
    out_lines: list[str] = []
    heredocs: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out_lines.append(line)
        matches = _find_unquoted_heredocs(line)
        i += 1
        for strip_tabs, delim in matches:
            body_lines: list[str] = []
            while i < len(lines):
                cand = lines[i]
                check = cand.lstrip("\t") if strip_tabs else cand
                if check == delim:
                    i += 1
                    break
                body_lines.append(cand)
                i += 1
            heredocs.append((line, "\n".join(body_lines)))
    return "\n".join(out_lines), heredocs


def _find_unquoted_heredocs(line: str) -> list[tuple[bool, str]]:
    """Find `<<EOF` / `<<'EOF'` / `<<-EOF` operators outside quotes (excluding `<<<`)."""
    results: list[tuple[bool, str]] = []
    quote: str | None = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "#":
            break
        if line.startswith("<<", i) and not line.startswith("<<<", i):
            m = _HEREDOC_OP_RE.match(line, i)
            if m:
                results.append((bool(m.group(1)), m.group(3)))
                i = m.end()
                continue
        i += 1
    return results


def _split_shell_segments(command: str, delimiters: tuple[str, ...]) -> list[str]:
    """Split a shell command on top-level unquoted delimiters, respecting quotes and escapes."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 1
                buf.append(command[i])
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if ch == "#" and (not buf or buf[-1].isspace()):
            # Skip shell comment to end of line
            while i < n and command[i] != "\n":
                i += 1
            continue
        matched_delim = None
        for d in delimiters:
            if command.startswith(d, i):
                if d == "|" and command.startswith("||", i):
                    continue
                matched_delim = d
                break
        if matched_delim:
            seg = "".join(buf).strip()
            if seg:
                segments.append(seg)
            buf = []
            i += len(matched_delim)
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return segments


def _scan_shell_redirections(segment: str) -> tuple[str, list[str]]:
    """Quote-aware scanner extracting true unquoted shell output redirections (`>`, `>>`, `&>`).

    Returns (segment_without_redirections, list_of_write_target_paths).
    Ignores `>`, `=>`, `->` inside single/double quotes, `<<`, `<<<`, `2>&1`, and `/dev/null`.
    """
    out: list[str] = []
    targets: list[str] = []
    quote: str | None = None
    i = 0
    n = len(segment)

    while i < n:
        ch = segment[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 1
                out.append(segment[i])
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(segment[i + 1])
            i += 2
            continue

        # Detect unquoted input redirection (`<<<`, `<<-`, `<<`, `<`) and strip it cleanly
        # so `cat <<EOF` never turns `<<EOF` into a fake filename.
        if ch == "<":
            if segment.startswith(("<<<", "<<-"), i):
                in_op_len = 3
            elif segment.startswith(("<<", "<&", "<>"), i):
                in_op_len = 2
            else:
                in_op_len = 1
            j = i + in_op_len
            while j < n and segment[j].isspace():
                j += 1
            tok_quote_in: str | None = None
            while j < n:
                c = segment[j]
                if tok_quote_in:
                    if c == "\\" and tok_quote_in == '"' and j + 1 < n:
                        j += 2
                        continue
                    if c == tok_quote_in:
                        tok_quote_in = None
                    j += 1
                    continue
                if c in ("'", '"'):
                    tok_quote_in = c
                    j += 1
                    continue
                if c.isspace() or c in (";", "|", "&", "<", ">"):
                    break
                j += 1
            out.append(" ")
            i = j
            continue

        # Detect unquoted output redirection: optional fd digit or `&` immediately before `>`
        redir_start = i
        op_len = 0
        is_stderr_only = False
        if ch == ">":
            op_len = 2 if segment.startswith((">>", ">|", ">&"), i) else 1
        elif ch == "&" and segment.startswith(("&>>", "&>"), i):
            op_len = 3 if segment.startswith("&>>", i) else 2
        elif (
            ch.isdigit()
            and (i == 0 or segment[i - 1].isspace())
            and i + 1 < n
            and segment[i + 1] == ">"
        ):
            is_stderr_only = ch == "2"
            op_len = 3 if segment.startswith((">>", ">|", ">&"), i + 1) else 2

        if op_len:
            op = segment[redir_start : redir_start + op_len]
            j = redir_start + op_len
            while j < n and segment[j].isspace():
                j += 1
            # Read the target token (respecting quotes)
            tok_buf: list[str] = []
            tok_quote: str | None = None
            while j < n:
                c = segment[j]
                if tok_quote:
                    if c == "\\" and tok_quote == '"' and j + 1 < n:
                        tok_buf.append(segment[j + 1])
                        j += 2
                        continue
                    if c == tok_quote:
                        tok_quote = None
                        j += 1
                        continue
                    tok_buf.append(c)
                    j += 1
                    continue
                if c in ("'", '"'):
                    tok_quote = c
                    j += 1
                    continue
                if c.isspace() or c in (";", "|", "&", "<", ">"):
                    break
                tok_buf.append(c)
                j += 1

            target = "".join(tok_buf).strip()
            if (
                target
                and not op.endswith(">&")
                and not target.startswith("&")
                and target != "/dev/null"
                and not is_stderr_only
            ):
                targets.append(target)
            out.append(" ")
            i = j
            continue

        out.append(ch)
        i += 1

    return "".join(out).strip(), targets


def _safe_shlex_split(segment: str) -> list[str]:
    """Linear O(n) quote-aware shell tokenizer (avoids CPython shlex quadratic behavior)."""
    tokens: list[str] = []
    buf: list[str] = []
    in_token = False
    quote: str | None = None
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                buf.append(ch)
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n and segment[i + 1] in ('"', "\\", "$", "`", "\n"):
                buf.append(segment[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            else:
                buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            in_token = True
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(segment[i + 1])
            in_token = True
            i += 2
            continue
        if ch.isspace():
            if in_token:
                tokens.append("".join(buf))
                buf = []
                in_token = False
            i += 1
            continue
        buf.append(ch)
        in_token = True
        i += 1
    if in_token:
        tokens.append("".join(buf))
    return tokens


def _resolve_path_with_cd(path: str | None, active_subdir: str | None) -> str | None:
    if not path:
        return active_subdir
    if not active_subdir:
        return path
    if path.startswith("/") or path.startswith("~"):
        return path
    joined = posixpath.normpath(posixpath.join(active_subdir, path))
    return joined


def _update_active_subdir(active_subdir: str | None, target: str | None) -> str | None:
    if not target or target in ("-", "~"):
        return None
    if target.startswith("/"):
        return posixpath.normpath(target)
    if active_subdir:
        return posixpath.normpath(posixpath.join(active_subdir, target))
    return posixpath.normpath(target)


def _extract_head_limit(pipe_segments: list[str]) -> int | None:
    """Detect trailing `| head -n N` or `| head -N` pipe caps."""
    for seg in pipe_segments[1:]:
        tokens = _safe_shlex_split(seg)
        if not tokens or Path(tokens[0]).name != "head":
            continue
        for idx, tok in enumerate(tokens[1:], start=1):
            if tok == "-n" and idx + 1 < len(tokens):
                val = safe_int(tokens[idx + 1])
                if val is not None and val > 0:
                    return val
            elif tok.startswith("-n") and len(tok) > 2:
                val = safe_int(tok[2:])
                if val is not None and val > 0:
                    return val
            elif tok.startswith("-") and tok[1:].isdigit():
                val = int(tok[1:])
                if val > 0:
                    return val
        return 10  # default `head` line count
    return None


def _strip_exit_code_lines(output: str) -> str:
    """Remove standalone `Exit code N` header/footer lines added by harnesses."""
    lines = split_lf_lines(output)
    while lines and _EXIT_CODE_LINE_RE.match(lines[0]):
        lines.pop(0)
    while lines and _EXIT_CODE_LINE_RE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines)


def _has_transforming_pipe(pipe_segments: list[str]) -> bool:
    for seg in pipe_segments[1:]:
        tokens = _safe_shlex_split(seg)
        if not tokens:
            continue
        if Path(tokens[0]).name not in _PASSTHROUGH_PIPE_CMDS:
            return True
    return False


_GREP_TOOL_ERROR_RE = re.compile(
    r"^(?:rg|grep|egrep|fgrep|ag|ack|git|find|fd)\s*:\s*",
    re.IGNORECASE,
)
_BINARY_MATCH_RE = re.compile(r"\bbinary\s+file\s+matches\b", re.IGNORECASE)
_SHELL_ERROR_LINE_RE = re.compile(
    r"^(?:fatal:\s+(?:not a git repository|ambiguous argument|bad revision|"
    r"unable to|cannot |invalid )"
    r"|(?:(?:/[^\s:]+/)?(?:zsh|bash|sh|dash|ksh|fish)|\(eval\))(?::[^:\n]+)*:\s*"
    r".*(?:command not found|not found|no such file or directory|permission denied))",
    re.IGNORECASE,
)
_FATAL_TOOL_ERROR_DETAIL_RE = re.compile(
    r"(?:no such file or directory|cannot open|is not a directory|"
    r"unrecognized option|invalid option|not a git repository|command not found|:\s*not found\s*$)",
    re.IGNORECASE,
)


def _is_search_error_line(line: str) -> bool:
    """Return True if a line is a tool/shell error (excluding grep binary-file match notices)."""
    s = line.strip()
    if not s or _BINARY_MATCH_RE.search(s):
        return False
    return bool(_GREP_TOOL_ERROR_RE.match(s) or _SHELL_ERROR_LINE_RE.match(s))


def _is_fatal_search_error_line(line: str) -> bool:
    """Return True if a line indicates a search command failed to execute or target was missing."""
    s = line.strip()
    if not s or _BINARY_MATCH_RE.search(s):
        return False
    if _SHELL_ERROR_LINE_RE.match(s):
        return True
    return bool(_GREP_TOOL_ERROR_RE.match(s) and _FATAL_TOOL_ERROR_DETAIL_RE.search(s))


def _parse_matched_files_from_grep_output(
    output: str,
    *,
    files_only_mode: bool = False,
    single_target_path: str | None = None,
) -> tuple[list[str], int, bool]:
    """Extract (matched_files, match_count, saw_tool_error) from direct grep/rg output."""
    cleaned = _strip_exit_code_lines(output)
    if not cleaned.strip():
        return [], 0, False
    files: list[str] = []
    seen: set[str] = set()
    match_lines = 0
    saw_tool_error = False
    for line in split_lf_lines(cleaned):
        s = line.strip()
        if not s or s == "--":
            continue
        if s.lower() in _NO_MATCH_OUTPUT_SENTINELS:
            continue
        if is_truncation_banner_line(s)[0]:
            continue
        if _EXIT_CODE_LINE_RE.match(s):
            continue
        if _is_search_error_line(s):
            saw_tool_error = True
            continue
        match_lines += 1
        # Handle `grep: path/to/x.pyc: binary file matches` cleanly as a match
        if _BINARY_MATCH_RE.search(s):
            stripped_bin = _GREP_TOOL_ERROR_RE.sub("", s, count=1)
            bin_path = stripped_bin.split(":", 1)[0].strip()
            if bin_path and " " not in bin_path and bin_path not in seen:
                seen.add(bin_path)
                files.append(bin_path)
            continue
        if ":" in s:
            prefix = s.split(":", 1)[0].strip()
            looks_like_path = (
                bool(prefix)
                and not prefix.isdigit()
                and " " not in prefix
                and (
                    single_target_path is None
                    or prefix == single_target_path
                    or "/" in prefix
                    or "." in prefix
                )
            )
            if looks_like_path and prefix not in seen:
                seen.add(prefix)
                files.append(prefix)
        elif " " not in s and ((files_only_mode and not s.isdigit()) or "/" in s) and s not in seen:
            seen.add(s)
            files.append(s)
    if match_lines > 0 and not files and single_target_path:
        files.append(single_target_path)
    return files, match_lines, saw_tool_error


def extract_apply_patch_edits(
    patch_text: str,
    *,
    event_id: str,
    event_index: int,
    tool_name: str,
    timestamp: str | None,
    raw_input: dict[str, Any],
    raw_output: str,
    is_error: bool,
    active_subdir: str | None = None,
) -> list[FileEditEvent]:
    """Parse `*** Update/Add/Delete/Move File:` headers from an apply_patch payload."""
    edits: list[FileEditEvent] = []
    for match in _PATCH_HEADER_RE.finditer(patch_text):
        action = match.group(1)
        raw_path = match.group(2).strip()
        file_path = _resolve_path_with_cd(raw_path, active_subdir) or raw_path
        if action == "Add File":
            op = "write"
        elif action == "Delete File":
            op = "delete"
        else:
            op = "edit"
        edits.append(
            FileEditEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=tool_name,
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
                is_error=is_error,
                has_result=True,
                file_path=file_path,
                operation=op,
            )
        )
    return edits


@dataclass
class ShellDerivedEvents:
    """Secondary typed events inferred from a shell command invocation."""

    file_reads: list[FileReadEvent] = field(default_factory=list)
    file_edits: list[FileEditEvent] = field(default_factory=list)
    searches: list[SearchEvent] = field(default_factory=list)


def analyze_shell_command(
    *,
    command: str,
    stdout: str,
    stderr: str = "",
    event_id: str,
    event_index: int,
    tool_name: str,
    timestamp: str | None,
    raw_input: dict[str, Any],
    raw_output: str,
    is_error: bool,
    exit_code: int | None,
    truncated: bool,
    truncation_reason: str | None,
) -> ShellDerivedEvents:
    """Infer FileReadEvent, SearchEvent, and FileEditEvent from common shell commands."""
    derived = ShellDerivedEvents()
    if not command:
        return derived

    # Strip heredoc bodies first so they aren't parsed as commands or redirects
    stripped_cmd, heredocs = _strip_heredocs(command)

    # Split compound commands (`&&`, `||`, `;`, `\n`) while tracking `cd <dir>`
    compounds = _split_shell_segments(stripped_cmd, ("&&", "||", ";", "\n"))
    active_subdir: str | None = None

    # Count non-cd commands to know whether stdout belongs uniquely to a single command
    non_cd_count = 0
    for comp in compounds:
        toks = _safe_shlex_split(comp)
        if toks and toks[0] != "cd":
            non_cd_count += 1
    sole_command = non_cd_count == 1

    # Queue heredoc bodies so `apply_patch <<'EOF'` consumes its body with live `active_subdir`
    heredoc_bodies = [body for _, body in heredocs]
    heredoc_cursor = 0

    for comp in compounds:
        pipe_segments = _split_shell_segments(comp, ("|",))
        if not pipe_segments:
            continue

        # Count heredoc operators present in this compound command before stripping them
        comp_heredocs: list[str] = []
        for _ in _find_unquoted_heredocs(comp):
            if heredoc_cursor < len(heredoc_bodies):
                comp_heredocs.append(heredoc_bodies[heredoc_cursor])
                heredoc_cursor += 1

        # Scan unquoted output redirections across pipe stages
        clean_first, first_redirects = _scan_shell_redirections(pipe_segments[0])
        all_redirects = list(first_redirects)
        clean_pipes = [clean_first]
        for seg in pipe_segments[1:]:
            c_seg, seg_redirs = _scan_shell_redirections(seg)
            clean_pipes.append(c_seg)
            all_redirects.extend(seg_redirs)

        first_tokens = _safe_shlex_split(clean_pipes[0])
        if not first_tokens:
            continue

        # Handle `cd <dir>` tracking
        if first_tokens[0] == "cd":
            target_dir = first_tokens[1] if len(first_tokens) >= 2 else None
            active_subdir = _update_active_subdir(active_subdir, target_dir)
            continue

        has_write_redirect = bool(all_redirects)
        if not is_error and exit_code in (None, 0):
            for target in all_redirects:
                resolved_target = _resolve_path_with_cd(target, active_subdir) or target
                derived.file_edits.append(
                    FileEditEvent(
                        event_id=event_id,
                        event_index=event_index,
                        tool_name=tool_name,
                        timestamp=timestamp,
                        raw_input=raw_input,
                        raw_output=raw_output,
                        has_result=True,
                        file_path=resolved_target,
                        operation="bash_modify",
                    )
                )

        cmd_name = Path(first_tokens[0]).name
        subcmd = first_tokens[1] if len(first_tokens) > 1 else ""

        if cmd_name == "apply_patch" and not is_error and exit_code in (None, 0):
            patch_sources = list(comp_heredocs)
            for arg in first_tokens[1:]:
                if "*** " in arg:
                    patch_sources.append(arg)
            for patch_body in patch_sources:
                derived.file_edits.extend(
                    extract_apply_patch_edits(
                        patch_body,
                        event_id=event_id,
                        event_index=event_index,
                        tool_name=f"{tool_name}:apply_patch",
                        timestamp=timestamp,
                        raw_input=raw_input,
                        raw_output=raw_output,
                        is_error=is_error,
                        active_subdir=active_subdir,
                    )
                )
            continue

        if cmd_name == "git" and subcmd == "grep":
            _analyze_search_tokens(
                derived=derived,
                search_cmd="git grep",
                tokens=first_tokens[2:],
                pipe_segments=clean_pipes,
                active_subdir=active_subdir,
                sole_command=sole_command,
                stdout=stdout,
                stderr=stderr,
                event_id=event_id,
                event_index=event_index,
                tool_name=tool_name,
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
                is_error=is_error,
                exit_code=exit_code,
                truncated=truncated,
                truncation_reason=truncation_reason,
            )
            continue

        if cmd_name == "git" and subcmd == "rm" and not is_error and exit_code in (None, 0):
            for tok in first_tokens[2:]:
                if not tok.startswith("-"):
                    resolved = _resolve_path_with_cd(tok, active_subdir) or tok
                    derived.file_edits.append(
                        FileEditEvent(
                            event_id=event_id,
                            event_index=event_index,
                            tool_name=tool_name,
                            timestamp=timestamp,
                            raw_input=raw_input,
                            raw_output=raw_output,
                            has_result=True,
                            file_path=resolved,
                            operation="delete",
                        )
                    )
            continue

        if not has_write_redirect and not is_error and exit_code in (None, 0):
            opaque_paths: list[str] = []
            if cmd_name == "git" and subcmd in _GIT_CONTENT_SUBCOMMANDS:
                opaque_paths = _git_displayed_paths(subcmd, first_tokens[2:])
            elif cmd_name in ("awk", "gawk", "mawk"):
                opaque_paths = _awk_input_paths(first_tokens[1:])
            for raw_path in opaque_paths:
                derived.file_reads.append(
                    FileReadEvent(
                        event_id=event_id,
                        event_index=event_index,
                        tool_name=f"{tool_name}:{cmd_name}",
                        timestamp=timestamp,
                        raw_input=raw_input,
                        raw_output=raw_output,
                        has_result=True,
                        truncated=truncated,
                        truncation_reason=truncation_reason,
                        file_path=_resolve_path_with_cd(raw_path, active_subdir) or raw_path,
                    )
                )

        if cmd_name in ("rg", "grep", "egrep", "fgrep", "ag", "ack"):
            _analyze_search_tokens(
                derived=derived,
                search_cmd=cmd_name,
                tokens=first_tokens[1:],
                pipe_segments=clean_pipes,
                active_subdir=active_subdir,
                sole_command=sole_command,
                stdout=stdout,
                stderr=stderr,
                event_id=event_id,
                event_index=event_index,
                tool_name=tool_name,
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
                is_error=is_error,
                exit_code=exit_code,
                truncated=truncated,
                truncation_reason=truncation_reason,
            )
            continue

        if cmd_name in ("fd", "find"):
            _analyze_find_tokens(
                derived=derived,
                search_cmd=cmd_name,
                tokens=first_tokens[1:],
                pipe_segments=clean_pipes,
                active_subdir=active_subdir,
                sole_command=sole_command,
                stdout=stdout,
                stderr=stderr,
                event_id=event_id,
                event_index=event_index,
                tool_name=tool_name,
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
                is_error=is_error,
                exit_code=exit_code,
                truncated=truncated,
                truncation_reason=truncation_reason,
            )
            continue

        if (
            not has_write_redirect
            and not is_error
            and exit_code in (None, 0)
            and cmd_name in ("cat", "nl", "head", "tail", "sed")
        ):
            _analyze_read_tokens(
                derived=derived,
                cmd_name=cmd_name,
                tokens=first_tokens[1:],
                pipe_segments=clean_pipes,
                active_subdir=active_subdir,
                sole_command=sole_command,
                stdout=stdout,
                event_id=event_id,
                event_index=event_index,
                tool_name=tool_name,
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
                truncated=truncated,
                truncation_reason=truncation_reason,
            )

        # File deletion / in-place modification commands
        if not is_error and exit_code in (None, 0):
            if cmd_name == "rm":
                for tok in first_tokens[1:]:
                    if not tok.startswith("-"):
                        resolved = _resolve_path_with_cd(tok, active_subdir) or tok
                        derived.file_edits.append(
                            FileEditEvent(
                                event_id=event_id,
                                event_index=event_index,
                                tool_name=tool_name,
                                timestamp=timestamp,
                                raw_input=raw_input,
                                raw_output=raw_output,
                                has_result=True,
                                file_path=resolved,
                                operation="delete",
                            )
                        )
            elif cmd_name == "sed" and any(
                t == "-i" or t.startswith("-i") for t in first_tokens[1:]
            ):
                _extract_sed_inplace_edits(
                    derived=derived,
                    tokens=first_tokens[1:],
                    active_subdir=active_subdir,
                    event_id=event_id,
                    event_index=event_index,
                    tool_name=tool_name,
                    timestamp=timestamp,
                    raw_input=raw_input,
                    raw_output=raw_output,
                )
            elif cmd_name in ("mv", "cp"):
                positionals = [t for t in first_tokens[1:] if not t.startswith("-")]
                if len(positionals) >= 2:
                    dst = _resolve_path_with_cd(positionals[-1], active_subdir) or positionals[-1]
                    derived.file_edits.append(
                        FileEditEvent(
                            event_id=event_id,
                            event_index=event_index,
                            tool_name=tool_name,
                            timestamp=timestamp,
                            raw_input=raw_input,
                            raw_output=raw_output,
                            has_result=True,
                            file_path=dst,
                            operation="bash_modify",
                        )
                    )
                    if cmd_name == "mv":
                        src = _resolve_path_with_cd(positionals[0], active_subdir) or positionals[0]
                        derived.file_edits.append(
                            FileEditEvent(
                                event_id=event_id,
                                event_index=event_index,
                                tool_name=tool_name,
                                timestamp=timestamp,
                                raw_input=raw_input,
                                raw_output=raw_output,
                                has_result=True,
                                file_path=src,
                                operation="delete",
                            )
                        )

        # Check `| tee <file>` in pipes
        if not is_error and exit_code in (None, 0):
            for seg in clean_pipes[1:]:
                p_toks = _safe_shlex_split(seg)
                if p_toks and Path(p_toks[0]).name == "tee":
                    for tok in p_toks[1:]:
                        if not tok.startswith("-"):
                            resolved = _resolve_path_with_cd(tok, active_subdir) or tok
                            derived.file_edits.append(
                                FileEditEvent(
                                    event_id=event_id,
                                    event_index=event_index,
                                    tool_name=tool_name,
                                    timestamp=timestamp,
                                    raw_input=raw_input,
                                    raw_output=raw_output,
                                    has_result=True,
                                    file_path=resolved,
                                    operation="bash_modify",
                                )
                            )

    # A target such as `$D/install.sh` names an unknown location (usually a scratch copy), so
    # it must not be matched by suffix against repository files the handoff cites.
    derived.file_edits = [e for e in derived.file_edits if not _has_unexpanded_shell_var(e)]
    return derived


def _has_unexpanded_shell_var(edit: FileEditEvent) -> bool:
    return "$" in edit.file_path or "`" in edit.file_path


_GIT_CONTENT_SUBCOMMANDS = frozenset({"show", "diff", "blame", "log"})
_GIT_NO_CONTENT_FLAGS = frozenset(
    {
        "--stat",
        "--shortstat",
        "--numstat",
        "--dirstat",
        "--name-only",
        "--name-status",
        "--summary",
        "--no-patch",
        "--quiet",
        "-s",
        "-q",
    }
)
_GLOB_CHARS = frozenset("*?[]{}")


def _looks_like_file_arg(token: str) -> bool:
    """Return True for a shell token that plausibly names one concrete file."""
    if not token or token.startswith(("-", ":", "$", "<", ">")) or _GLOB_CHARS & set(token):
        return False
    base = token.rstrip("/").rsplit("/", 1)[-1]
    return "." in base.strip(".") or base in ("Makefile", "Dockerfile", "LICENSE")


def _git_displayed_paths(subcmd: str, tokens: list[str]) -> list[str]:
    """Return files whose content `git show|diff|blame|log` printed, per the command line.

    The output cannot be mapped back to line numbers (other revisions, piped through
    `sed`/`grep`), so callers record these as reads with an unknown line window: enough to
    know the agent looked at the file, not enough to vouch for a specific line.
    """
    flags = {t.split("=", 1)[0] for t in tokens if t.startswith("-")}
    if flags & _GIT_NO_CONTENT_FLAGS:
        return []
    if subcmd == "log" and not flags & {"-p", "-u", "--patch", "-L"}:
        return []
    paths: list[str] = []
    if "--" in tokens:
        paths.extend(t for t in tokens[tokens.index("--") + 1 :] if _looks_like_file_arg(t))
        tokens = tokens[: tokens.index("--")]
    if subcmd == "show":
        for tok in tokens:
            if not tok.startswith("-") and ":" in tok:
                candidate = tok.split(":", 1)[1]
                if _looks_like_file_arg(candidate):
                    paths.append(candidate)
    elif subcmd == "blame":
        positionals = [t for t in tokens if _looks_like_file_arg(t)]
        if positionals:
            paths.append(positionals[-1])
    return paths


def _awk_input_paths(tokens: list[str]) -> list[str]:
    """Return the input files of an `awk 'program' file...` invocation."""
    positionals: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-F", "-v", "-f") and i + 1 < len(tokens):
            if tok == "-f":
                positionals.append("")  # program comes from a file, so no inline program token
            i += 2
            continue
        if tok.startswith("-") and len(tok) > 1:
            i += 1
            continue
        positionals.append(tok)
        i += 1
    return [t for t in positionals[1:] if _looks_like_file_arg(t) and "=" not in t]


def _extract_sed_inplace_edits(
    *,
    derived: ShellDerivedEvents,
    tokens: list[str],
    active_subdir: str | None,
    event_id: str,
    event_index: int,
    tool_name: str,
    timestamp: str | None,
    raw_input: dict[str, Any],
    raw_output: str,
) -> None:
    """Extract target file paths from `sed -i [ext] <expr> <files...>` (GNU & BSD/macOS)."""
    has_e_flag = False
    positionals: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-e" and i + 1 < len(tokens):
            has_e_flag = True
            i += 2
            continue
        if tok == "-f" and i + 1 < len(tokens):
            has_e_flag = True
            i += 2
            continue
        if tok == "-i":
            # BSD/macOS `sed -i '' 's/a/b/' file` passes an explicit backup suffix (often `""`)
            if i + 1 < len(tokens) and (tokens[i + 1] == "" or tokens[i + 1].startswith(".")):
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        positionals.append(tok)
        i += 1

    targets = positionals if has_e_flag else positionals[1:]
    for target in targets:
        if not target:
            continue
        resolved = _resolve_path_with_cd(target, active_subdir) or target
        derived.file_edits.append(
            FileEditEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=tool_name,
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
                has_result=True,
                file_path=resolved,
                operation="bash_modify",
            )
        )


_SEARCH_NON_MATCH_OUTPUT_FLAGS = frozenset(
    {
        "-c",
        "--count",
        "--count-matches",
        "-q",
        "--quiet",
        "--silent",
        "--json",
        "--stats",
        "-v",
        "--invert-match",
        "-L",
        "--files-without-match",
    }
)
_FIND_VALUE_FLAGS = frozenset(
    {
        "-type",
        "-xtype",
        "-maxdepth",
        "-mindepth",
        "-path",
        "-ipath",
        "-wholename",
        "-iwholename",
        "-lname",
        "-ilname",
        "-regex",
        "-iregex",
        "-regextype",
        "-size",
        "-mtime",
        "-ctime",
        "-atime",
        "-mmin",
        "-cmin",
        "-amin",
        "-newer",
        "-anewer",
        "-cnewer",
        "-user",
        "-group",
        "-uid",
        "-gid",
        "-perm",
        "-links",
        "-inum",
        "-samefile",
        "-fstype",
        "-printf",
        "-fprintf",
        "-fprint",
        "-fprint0",
        "-fls",
    }
)
_FIND_STOP_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FD_VALUE_FLAGS = frozenset(
    {
        "-e",
        "--extension",
        "-t",
        "--type",
        "-d",
        "--max-depth",
        "--min-depth",
        "--exact-depth",
        "-c",
        "--color",
        "-j",
        "--threads",
        "--changed-within",
        "--changed-before",
        "-S",
        "--size",
        "-o",
        "--owner",
        "--base-directory",
        "--path-separator",
        "--max-results",
        "--format",
        "--ignore-file",
        "--strip-cwd-prefix",
    }
)
_FD_STOP_FLAGS = frozenset({"-x", "--exec", "-X", "--exec-batch"})


def _analyze_search_tokens(
    *,
    derived: ShellDerivedEvents,
    search_cmd: str,
    tokens: list[str],
    pipe_segments: list[str],
    active_subdir: str | None,
    sole_command: bool,
    stdout: str,
    stderr: str = "",
    event_id: str,
    event_index: int,
    tool_name: str,
    timestamp: str | None,
    raw_input: dict[str, Any],
    raw_output: str,
    is_error: bool,
    exit_code: int | None,
    truncated: bool,
    truncation_reason: str | None,
) -> None:
    # Only treat an errored search as "no matches" when exit_code is explicitly 1
    # AND stdout/stderr carry no error text (e.g. failed `cd` or missing path).
    combined_err_stream = f"{stdout}\n{stderr}" if stderr else stdout
    if is_error:
        if exit_code != 1:
            return
        cleaned_err = _strip_exit_code_lines(combined_err_stream).strip()
        if cleaned_err:
            return
    elif exit_code not in (0, 1, None):
        return
    elif exit_code == 1:
        cleaned_err = _strip_exit_code_lines(combined_err_stream).strip()
        if cleaned_err and any(_is_search_error_line(line) for line in split_lf_lines(cleaned_err)):
            return

    if search_cmd == "rg":
        value_flags = _COMMON_SEARCH_VALUE_FLAGS | _RG_ONLY_VALUE_FLAGS
    elif search_cmd in ("ag", "ack"):
        value_flags = _COMMON_SEARCH_VALUE_FLAGS | _AG_ACK_VALUE_FLAGS
    else:
        value_flags = _COMMON_SEARCH_VALUE_FLAGS

    short_value_chars = {f[1] for f in value_flags if len(f) == 2 and f.startswith("-")} | {
        "e",
        "m",
    }
    if search_cmd == "rg":
        short_value_chars.add("g")

    non_match_flags = (
        _SEARCH_NON_MATCH_OUTPUT_FLAGS - {"-L"}
        if search_cmd == "rg"
        else _SEARCH_NON_MATCH_OUTPUT_FLAGS
    )

    query: str | None = None
    raw_paths: list[str] = []
    glob_pattern: str | None = None
    excluded_dirs: list[str] = []
    files_only_mode = False
    count_unknown = False
    result_limit = _extract_head_limit(pipe_segments)

    i = 0
    end_of_flags = False
    while i < len(tokens):
        tok = tokens[i]
        if not end_of_flags and tok == "--":
            end_of_flags = True
            i += 1
            continue
        if not end_of_flags and tok.startswith("-"):
            if tok in non_match_flags:
                count_unknown = True
                i += 1
                continue
            if tok in ("-l", "--files-with-matches", "--files"):
                files_only_mode = True
                i += 1
                continue
            if tok in ("-e", "--regexp") and i + 1 < len(tokens):
                query = tokens[i + 1]
                i += 2
                continue
            if tok.startswith("--regexp="):
                query = tok.split("=", 1)[1]
                i += 1
                continue
            if tok in ("-g", "--glob", "--iglob", "--include") and i + 1 < len(tokens):
                val = tokens[i + 1]
                if val.startswith("!"):
                    excluded_dirs.append(val[1:].strip("/").removesuffix("/**"))
                else:
                    glob_pattern = val
                i += 2
                continue
            if tok.startswith(("--glob=", "--iglob=", "--include=")):
                val = tok.split("=", 1)[1]
                if val.startswith("!"):
                    excluded_dirs.append(val[1:].strip("/").removesuffix("/**"))
                else:
                    glob_pattern = val
                i += 1
                continue
            if tok in ("--exclude-dir", "--exclude", "--ignore") and i + 1 < len(tokens):
                excluded_dirs.append(tokens[i + 1])
                i += 2
                continue
            if tok.startswith(("--exclude-dir=", "--exclude=", "--ignore=")):
                excluded_dirs.append(tok.split("=", 1)[1])
                i += 1
                continue
            if tok in ("-m", "--max-count") and i + 1 < len(tokens):
                val_int = safe_int(tokens[i + 1])
                if val_int is not None and val_int > 0 and result_limit is None:
                    result_limit = val_int
                i += 2
                continue
            if tok.startswith("--max-count="):
                val_int = safe_int(tok.split("=", 1)[1])
                if val_int is not None and val_int > 0 and result_limit is None:
                    result_limit = val_int
                i += 1
                continue
            if tok.startswith("-m") and len(tok) > 2 and tok[2:].isdigit():
                val_int = int(tok[2:])
                if val_int > 0 and result_limit is None:
                    result_limit = val_int
                i += 1
                continue
            if tok in value_flags and i + 1 < len(tokens):
                i += 2
                continue
            # Combined short flags: walk left to right; the first value-taking char ends
            # the cluster (remainder is its glued value, e.g. `-trust`, `-g!vendor`).
            # Only consume `tokens[i + 1]` if the value char is last (e.g. `-rnA 3`).
            if not tok.startswith("--") and len(tok) > 2:
                short_non_match = ("q", "c", "v") if search_cmd == "rg" else ("q", "c", "v", "L")
                flags_body = tok[1:]
                consumed_next = False
                for idx_c, ch in enumerate(flags_body):
                    if ch in short_value_chars:
                        glued = flags_body[idx_c + 1 :]
                        if glued:
                            if ch == "e":
                                query = glued
                            elif ch == "g":
                                if glued.startswith("!"):
                                    excluded_dirs.append(glued[1:].strip("/").removesuffix("/**"))
                                else:
                                    glob_pattern = glued
                            elif ch == "m" and glued.isdigit() and result_limit is None:
                                val_m = int(glued)
                                if val_m > 0:
                                    result_limit = val_m
                        elif i + 1 < len(tokens):
                            if ch == "e":
                                query = tokens[i + 1]
                            elif ch == "g":
                                nxt = tokens[i + 1]
                                if nxt.startswith("!"):
                                    excluded_dirs.append(nxt[1:].strip("/").removesuffix("/**"))
                                else:
                                    glob_pattern = nxt
                            elif ch == "m" and result_limit is None:
                                val_m_opt = safe_int(tokens[i + 1])
                                if val_m_opt is not None and val_m_opt > 0:
                                    result_limit = val_m_opt
                            consumed_next = True
                        break
                    if ch in short_non_match:
                        count_unknown = True
                    if ch == "l":
                        files_only_mode = True
                i += 2 if consumed_next else 1
                continue
            i += 1
            continue

        if query is None:
            query = tok
        else:
            raw_paths.append(tok)
        i += 1

    # Resolve all search paths against active `cd` subdir
    resolved_paths: list[str] = []
    if raw_paths:
        for rp in raw_paths:
            res = _resolve_path_with_cd(rp, active_subdir)
            if res:
                resolved_paths.append(res)
    elif active_subdir:
        resolved_paths.append(active_subdir)

    # Determine whether stdout can be directly interpreted as grep output
    transforming_pipe = _has_transforming_pipe(pipe_segments)
    direct_output = sole_command and not transforming_pipe and not count_unknown

    matched_files: list[str]
    match_count: int | None
    saw_tool_error = False
    if direct_output:
        single_target = resolved_paths[0] if len(resolved_paths) == 1 else None
        matched_files, parsed_count, saw_tool_error = _parse_matched_files_from_grep_output(
            stdout,
            files_only_mode=files_only_mode,
            single_target_path=single_target,
        )
        if stderr and any(_is_search_error_line(line) for line in split_lf_lines(stderr)):
            saw_tool_error = True
        if saw_tool_error and parsed_count == 0:
            # Complete failure (e.g. missing path, command not found, git fatal error):
            # never record a failed search as a clean zero-match search.
            return
        match_count = None if saw_tool_error else parsed_count
    else:
        # Even on non-direct pipes (`| wc -l`, `|| true`, `; echo done`, `-rc | head`),
        # abort if stdout/stderr has a fatal command/path error so missing roots aren't recorded.
        if any(_is_fatal_search_error_line(line) for line in split_lf_lines(combined_err_stream)):
            return
        matched_files = []
        match_count = None

    is_trunc, trunc_reason = (truncated, truncation_reason)
    if saw_tool_error and not is_trunc:
        is_trunc = True
        trunc_reason = "search tool error encountered (incomplete results)"
    if not is_trunc:
        is_trunc, trunc_reason = detect_truncation(
            stdout if direct_output else "",
            tool_name=search_cmd,
            result_limit=result_limit,
            observed_count=match_count,
        )

    derived.searches.append(
        SearchEvent(
            event_id=event_id,
            event_index=event_index,
            tool_name=f"{tool_name}:{search_cmd}",
            timestamp=timestamp,
            raw_input=raw_input,
            raw_output=raw_output,
            is_error=False,
            has_result=True,
            truncated=is_trunc,
            truncation_reason=trunc_reason,
            query=query or "",
            search_path=resolved_paths[0] if resolved_paths else None,
            search_paths=resolved_paths,
            glob_pattern=glob_pattern,
            excluded_dirs=excluded_dirs,
            match_count=match_count,
            matched_files=matched_files,
            output=stdout if direct_output else "",
            result_limit=result_limit,
        )
    )


def _analyze_find_tokens(
    *,
    derived: ShellDerivedEvents,
    search_cmd: str,
    tokens: list[str],
    pipe_segments: list[str],
    active_subdir: str | None,
    sole_command: bool,
    stdout: str,
    stderr: str = "",
    event_id: str,
    event_index: int,
    tool_name: str,
    timestamp: str | None,
    raw_input: dict[str, Any],
    raw_output: str,
    is_error: bool,
    exit_code: int | None,
    truncated: bool,
    truncation_reason: str | None,
) -> None:
    if is_error or exit_code not in (0, None):
        return

    raw_paths: list[str] = []
    glob_pattern: str | None = None
    excluded_dirs: list[str] = []
    result_limit = _extract_head_limit(pipe_segments)

    if search_cmd == "find":
        # POSIX `find [-H|-L|-P] [path...] [expression...]`:
        # paths appear strictly before the first expression operator (`-`, `!`, `(`, `)`, `,`).
        i = 0
        while i < len(tokens) and tokens[i] in ("-H", "-L", "-P"):
            i += 1
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("-") or tok in ("!", "(", ")", ","):
                break
            raw_paths.append(tok)
            i += 1

        # Pre-compute which parenthesized groups are followed by `-prune`
        pruned_indices: set[int] = set()
        paren_stack: list[int] = []
        for idx, t in enumerate(tokens):
            if t == "(":
                paren_stack.append(idx)
            elif t == ")" and paren_stack:
                start_p = paren_stack.pop()
                if idx + 1 < len(tokens) and tokens[idx + 1] == "-prune":
                    pruned_indices.update(range(start_p, idx + 1))

        negated = False
        while i < len(tokens):
            tok = tokens[i]
            if tok in _FIND_STOP_FLAGS:
                break
            if tok in ("-not", "!"):
                negated = True
                i += 1
                continue
            if tok in ("-name", "-iname") and i + 1 < len(tokens):
                is_pruned = (i in pruned_indices) or (
                    i + 2 < len(tokens) and tokens[i + 2] == "-prune"
                )
                if negated or is_pruned:
                    cleaned = tokens[i + 1].strip("*/")
                    if cleaned:
                        excluded_dirs.append(cleaned)
                else:
                    glob_pattern = tokens[i + 1]
                negated = False
                i += 2
                continue
            if tok in ("-path", "-ipath", "-wholename", "-iwholename") and i + 1 < len(tokens):
                is_pruned = (i in pruned_indices) or (
                    i + 2 < len(tokens) and tokens[i + 2] == "-prune"
                )
                if negated or is_pruned:
                    cleaned_pat = tokens[i + 1].removeprefix("./").strip("*/")
                    if cleaned_pat:
                        excluded_dirs.append(cleaned_pat)
                negated = False
                i += 2
                continue
            if tok in _FIND_VALUE_FLAGS and i + 1 < len(tokens):
                negated = False
                i += 2
                continue
            negated = False
            i += 1
    else:
        # `fd [FLAGS/OPTIONS] [<pattern>] [<path>...]`
        i = 0
        end_of_flags = False
        while i < len(tokens):
            tok = tokens[i]
            if not end_of_flags and tok == "--":
                end_of_flags = True
                i += 1
                continue
            if not end_of_flags and tok in _FD_STOP_FLAGS:
                break
            if not end_of_flags and tok in ("-E", "--exclude") and i + 1 < len(tokens):
                excluded_dirs.append(tokens[i + 1].strip("*/"))
                i += 2
                continue
            if not end_of_flags and tok.startswith("--exclude="):
                excluded_dirs.append(tok.split("=", 1)[1].strip("*/"))
                i += 1
                continue
            if not end_of_flags and tok == "--search-path" and i + 1 < len(tokens):
                raw_paths.append(tokens[i + 1])
                i += 2
                continue
            if not end_of_flags and tok.startswith("--search-path="):
                raw_paths.append(tok.split("=", 1)[1])
                i += 1
                continue
            if not end_of_flags and tok in _FD_VALUE_FLAGS and i + 1 < len(tokens):
                i += 2
                continue
            if not end_of_flags and tok.startswith("-"):
                i += 1
                continue
            if glob_pattern is None:
                glob_pattern = tok
            else:
                raw_paths.append(tok)
            i += 1

    resolved_paths: list[str] = []
    if raw_paths:
        for rp in raw_paths:
            res = _resolve_path_with_cd(rp, active_subdir)
            if res:
                resolved_paths.append(res)
    elif active_subdir:
        resolved_paths.append(active_subdir)

    direct_output = sole_command and not _has_transforming_pipe(pipe_segments)
    cleaned_stdout = _strip_exit_code_lines(stdout)
    combined_err_stream = f"{cleaned_stdout}\n{stderr}" if stderr else cleaned_stdout
    lines: list[str] = []
    saw_find_error = bool(
        stderr and any(_is_search_error_line(line) for line in split_lf_lines(stderr))
    )
    if direct_output:
        for line in split_lf_lines(cleaned_stdout):
            s = line.strip()
            if not s:
                continue
            if is_truncation_banner_line(s)[0]:
                continue
            if _is_search_error_line(s):
                saw_find_error = True
                continue
            lines.append(s)
        if saw_find_error and not lines:
            return
    elif any(_is_fatal_search_error_line(line) for line in split_lf_lines(combined_err_stream)):
        return
    match_count: int | None = len(lines) if (direct_output and not saw_find_error) else None

    is_trunc, trunc_reason = (truncated, truncation_reason)
    if not is_trunc:
        is_trunc, trunc_reason = detect_truncation(
            cleaned_stdout if direct_output else "",
            tool_name=search_cmd,
            result_limit=result_limit,
            observed_count=match_count,
        )

    derived.searches.append(
        SearchEvent(
            event_id=event_id,
            event_index=event_index,
            tool_name=f"{tool_name}:{search_cmd}",
            timestamp=timestamp,
            raw_input=raw_input,
            raw_output=raw_output,
            is_error=False,
            has_result=True,
            truncated=is_trunc,
            truncation_reason=trunc_reason,
            query=glob_pattern or "",
            search_path=resolved_paths[0] if resolved_paths else None,
            search_paths=resolved_paths,
            glob_pattern=glob_pattern,
            excluded_dirs=excluded_dirs,
            match_count=match_count,
            matched_files=lines,
            output=cleaned_stdout if direct_output else "",
            result_limit=result_limit,
        )
    )


def _pipe_allows_exact_read_window(pipe_segments: list[str]) -> tuple[bool, int | None]:
    """Return (is_exact_window, head_limit) for a read command's downstream pipe stages.

    Only unpiped reads or a single `| head -n K` preserve 1-based line windows.
    Piping through `grep`, `tail`, `awk`, `sed`, etc. breaks the line-number mapping.
    """
    if len(pipe_segments) == 1:
        return True, None
    if len(pipe_segments) == 2:
        tokens = _safe_shlex_split(pipe_segments[1])
        if tokens and Path(tokens[0]).name == "head":
            return True, _extract_head_limit(pipe_segments)
    return False, None


def _analyze_read_tokens(
    *,
    derived: ShellDerivedEvents,
    cmd_name: str,
    tokens: list[str],
    pipe_segments: list[str],
    active_subdir: str | None,
    sole_command: bool,
    stdout: str,
    event_id: str,
    event_index: int,
    tool_name: str,
    timestamp: str | None,
    raw_input: dict[str, Any],
    raw_output: str,
    truncated: bool,
    truncation_reason: str | None,
) -> None:
    exact_pipe, pipe_head_limit = _pipe_allows_exact_read_window(pipe_segments)
    can_infer_window = sole_command and exact_pipe

    if cmd_name == "sed":
        if "-n" not in tokens or any(t == "-i" or t.startswith("-i") for t in tokens):
            return
        positionals = [t for t in tokens if not t.startswith("-")]
        if len(positionals) != 2:
            return
        expr, raw_path = positionals
        m = _SED_RANGE_RE.match(expr.strip())
        if not m:
            return
        file_path = _resolve_path_with_cd(raw_path, active_subdir) or raw_path
        start = int(m.group(1))
        end_tok = m.group(2)
        if not can_infer_window:
            derived.file_reads.append(
                FileReadEvent(
                    event_id=event_id,
                    event_index=event_index,
                    tool_name=f"{tool_name}:sed",
                    timestamp=timestamp,
                    raw_input=raw_input,
                    raw_output=raw_output,
                    has_result=True,
                    truncated=truncated,
                    truncation_reason=truncation_reason,
                    file_path=file_path,
                    start_line=None,
                    end_line=None,
                    offset=start,
                    limit=None,
                    content="",
                    line_count=0,
                )
            )
            return

        explicit_end = int(end_tok) if (end_tok and end_tok != "$") else None
        req_limit = (explicit_end - start + 1) if explicit_end is not None else pipe_head_limit
        parsed = parse_numbered_lines(
            stdout, offset=start, limit=req_limit, expect_line_numbers=False
        )
        start_line: int | None
        end_line: int | None
        if parsed.start_line is None or parsed.end_line is None:
            start_line = None
            end_line = None
        else:
            start_line = start if parsed.line_count > 0 else None
            if explicit_end is not None:
                end_line = min(explicit_end, parsed.end_line)
            else:
                end_line = parsed.end_line

        derived.file_reads.append(
            FileReadEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=f"{tool_name}:sed",
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
                has_result=True,
                truncated=truncated or parsed.truncated,
                truncation_reason=truncation_reason or parsed.truncation_reason,
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                offset=start,
                limit=req_limit,
                content=parsed.content,
                line_count=parsed.line_count,
            )
        )
        return

    # `cat`, `nl`, `head`, `tail`
    limit: int | None = None
    has_line_num_flag = cmd_name == "nl"
    files: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if cmd_name in ("head", "tail"):
            if tok in ("-n", "-c") and i + 1 < len(tokens):
                if tok == "-n":
                    limit = safe_int(tokens[i + 1].lstrip("+"))
                i += 2
                continue
            if tok.startswith("-n") and len(tok) > 2:
                limit = safe_int(tok[2:].lstrip("+"))
                i += 1
                continue
            if tok.startswith("-") and tok[1:].isdigit():
                limit = int(tok[1:])
                i += 1
                continue
        elif (
            cmd_name == "nl"
            and tok in ("-b", "-h", "-f", "-s", "-v", "-i", "-w", "-n")
            and i + 1 < len(tokens)
        ):
            i += 2
            continue
        elif cmd_name == "cat" and tok in ("-n", "-b", "--number", "--number-nonblank"):
            has_line_num_flag = True
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        files.append(tok)
        i += 1

    if not files:
        return

    # Multiple files in one `cat a.py b.py`: emit unknown-window FileReadEvents for each
    if len(files) > 1 or not can_infer_window:
        for raw_path in files:
            file_path = _resolve_path_with_cd(raw_path, active_subdir) or raw_path
            derived.file_reads.append(
                FileReadEvent(
                    event_id=event_id,
                    event_index=event_index,
                    tool_name=f"{tool_name}:{cmd_name}",
                    timestamp=timestamp,
                    raw_input=raw_input,
                    raw_output=raw_output,
                    has_result=True,
                    truncated=truncated,
                    truncation_reason=truncation_reason,
                    file_path=file_path,
                    start_line=None,
                    end_line=None,
                    offset=None,
                    limit=limit,
                    content="",
                    line_count=0,
                )
            )
        return

    file_path = _resolve_path_with_cd(files[0], active_subdir) or files[0]
    if cmd_name == "head" and limit is None:
        limit = 10
    if cmd_name == "tail":
        # Without knowing total file lines, tail cannot establish 1-based start_line/end_line
        parsed = parse_numbered_lines(stdout, expect_line_numbers=False)
        derived.file_reads.append(
            FileReadEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=f"{tool_name}:tail",
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
                has_result=True,
                truncated=truncated or parsed.truncated,
                truncation_reason=truncation_reason or parsed.truncation_reason,
                file_path=file_path,
                start_line=None,
                end_line=None,
                offset=None,
                limit=limit,
                content=parsed.content,
                line_count=parsed.line_count,
            )
        )
        return

    eff_limit = limit if limit is not None else pipe_head_limit
    parsed = parse_numbered_lines(
        stdout,
        offset=1,
        limit=eff_limit,
        expect_line_numbers=has_line_num_flag,
    )
    derived.file_reads.append(
        FileReadEvent(
            event_id=event_id,
            event_index=event_index,
            tool_name=f"{tool_name}:{cmd_name}",
            timestamp=timestamp,
            raw_input=raw_input,
            raw_output=raw_output,
            has_result=True,
            truncated=truncated or parsed.truncated,
            truncation_reason=truncation_reason or parsed.truncation_reason,
            file_path=file_path,
            start_line=parsed.start_line,
            end_line=parsed.end_line,
            offset=1,
            limit=eff_limit,
            content=parsed.content,
            line_count=parsed.line_count,
        )
    )

"""Shared JSONL loading, line-range, truncation, pagination, and shell helpers."""

from __future__ import annotations

import json
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


@dataclass
class JsonlLoadResult:
    """Result of reading a JSONL source with malformed-line resilience."""

    records: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    malformed_lines: int = 0
    parse_warnings: list[str] = field(default_factory=list)


def load_jsonl_lines(lines: Iterable[str]) -> JsonlLoadResult:
    """Parse JSONL lines into dict records, never crashing on malformed lines."""
    result = JsonlLoadResult()
    suppressed = 0
    for line_no, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            result.malformed_lines += 1
            if len(result.parse_warnings) < MAX_PARSE_WARNINGS:
                result.parse_warnings.append(f"Line {line_no}: invalid JSON ({exc.msg})")
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
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return load_jsonl_lines(fh), str(path)
    return load_jsonl_lines(source), None


def safe_int(value: Any) -> int | None:
    """Convert an int-like value to int, rejecting booleans."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s and (s.isdigit() or (s.startswith("-") and s[1:].isdigit())):
            return int(s)
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
        return json.dumps(content, ensure_ascii=False)
    return str(content)


# --- Line-numbered read output parsing ---

_SYSTEM_REMINDER_RE = re.compile(r"\n*<system-reminder>.*?</system-reminder>\s*", re.DOTALL)
_ARROW_LINE_RE = re.compile(r"^\s*(\d+)→(.*)$")
_TAB_LINE_RE = re.compile(r"^\s*(\d+)\t(.*)$")
_TRAILING_NOTICE_RE = re.compile(
    r"^(?:\.\.\.|\[.*truncated.*\]|Note:|Showing\s+|File\s+content\s+truncated|"
    r"\(\d+\s+more\s+lines?\)|\(End of file)",
    re.IGNORECASE,
)


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
) -> ParsedReadOutput:
    """Parse Claude Code / cat -n line-numbered output into clean content and line bounds."""
    cleaned_raw = _SYSTEM_REMINDER_RE.sub("", raw_output)
    if not cleaned_raw:
        return ParsedReadOutput(content="", start_line=None, end_line=None, line_count=0)

    lines = cleaned_raw.splitlines()
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

    # Try arrow format first (Claude Code's `   12→code`), then tab format (`   12\tcode`)
    for regex in (_ARROW_LINE_RE, _TAB_LINE_RE):
        parsed_nums: list[int] = []
        parsed_text: list[str] = []
        matched = True
        prev_num = 0
        for line in effective_lines:
            m = regex.match(line)
            if not m:
                matched = False
                break
            num = int(m.group(1))
            if num <= prev_num:
                matched = False
                break
            prev_num = num
            parsed_nums.append(num)
            parsed_text.append(m.group(2))
        if matched and parsed_nums:
            # Check for explicit line-length truncation inside Claude Code Read notices
            if not truncated:
                # Only check non-code reminders from original raw_output (if any)
                for reminder in _SYSTEM_REMINDER_RE.findall(raw_output):
                    r_trunc, r_reason = detect_truncation(reminder, tool_name="Read")
                    if r_trunc:
                        truncated = True
                        truncation_reason = r_reason
                        break
            return ParsedReadOutput(
                content="\n".join(parsed_text),
                start_line=parsed_nums[0],
                end_line=parsed_nums[-1],
                line_count=len(parsed_nums),
                truncated=truncated,
                truncation_reason=truncation_reason,
            )

    # Unnumbered output: infer bounds from offset if available, otherwise 1..N
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
    start_line = offset if (offset is not None and offset >= 1) else 1
    end_line = start_line + line_count - 1
    return ParsedReadOutput(
        content="\n".join(effective_lines),
        start_line=start_line,
        end_line=end_line,
        line_count=line_count,
        truncated=truncated,
        truncation_reason=truncation_reason,
    )


# --- Truncation detection ---

_SHOWING_FIRST_RE = re.compile(
    r"\bshowing\s+(?:the\s+)?first\s+(\d+)\s+(?:of|out\s+of)\s+(\d+)\b",
    re.IGNORECASE,
)
_SHOWING_RANGE_RE = re.compile(
    r"\bshowing\s+(?:lines?|results?|items?)\s+(\d+)\s*[-–]\s*(\d+)\s+of\s+(\d+)\b",
    re.IGNORECASE,
)
_TRUNCATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\[[^\]]*\boutput\s+truncated\b[^\]]*\]", re.IGNORECASE), "output truncated"),
    (re.compile(r"\[[^\]]*\btruncated\b[^\]]*\]", re.IGNORECASE), "output truncated"),
    (re.compile(r"(?im)^\s*(?:\.\.\.\s*)?output\s+(?:was\s+)?truncated\b"), "output truncated"),
    (re.compile(r"\boutput\s+(?:was\s+)?truncated\b", re.IGNORECASE), "output truncated"),
    (re.compile(r"\bresults?\s+(?:were\s+)?truncated\b", re.IGNORECASE), "results truncated"),
    (re.compile(r"\bfile\s+content\s+truncated\b", re.IGNORECASE), "file content truncated"),
    (
        re.compile(r"\b(?:output|response|content)\s+exceeded\b", re.IGNORECASE),
        "output limit exceeded",
    ),
    (re.compile(r"\blimit\s+reached\b", re.IGNORECASE), "limit reached"),
    (re.compile(r"\bhead_limit\b[^\n]*\breached\b", re.IGNORECASE), "head_limit reached"),
    (
        re.compile(
            r"(?:\.\.\.\s*\(?|\()\s*(\d+)\s+more\s+"
            r"(?:lines?|matches?|results?|files?|items?|chars?|characters?|bytes?)\s*\)?",
            re.IGNORECASE,
        ),
        "additional output omitted",
    ),
    (
        re.compile(
            r"\(\s*\d+\s+(?:lines?|matches?|results?|chars?|characters?)\s+"
            r"(?:omitted|truncated|skipped)\s*\)",
            re.IGNORECASE,
        ),
        "output omitted",
    ),
    (
        re.compile(
            r"\btruncated\s+(?:to|after)\s+\d+\s+"
            r"(?:lines?|chars?|characters?|bytes?|matches?|results?)\b",
            re.IGNORECASE,
        ),
        "output truncated to cap",
    ),
    (
        re.compile(r"(?im)^\s*truncated\s+\d+\s+(?:lines?|chars?|bytes?)\b"),
        "output truncated",
    ),
    (
        re.compile(
            r"\b(?:longer|more)\s+than\s+(?:the\s+)?(?:maximum|max|limit)\b",
            re.IGNORECASE,
        ),
        "output exceeded maximum length",
    ),
)


def _try_parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def detect_truncation(
    text: str,
    *,
    tool_name: str = "",
    result_limit: int | None = None,
    observed_count: int | None = None,
) -> tuple[bool, str | None]:
    """Detect explicit truncation markers in tool outputs without noisy false positives."""
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
            # Don't run free-text regexes over arbitrary JSON string values unless explicit
            # banners appear; fall through only for explicit banner messages in top-level message
            msg = container.get("truncation_reason") or container.get("warning")
            if isinstance(msg, str) and "truncat" in msg.lower():
                return True, msg.strip()
        return False, None

    # Check "showing first N of M" only when N < M
    m_first = _SHOWING_FIRST_RE.search(text)
    if m_first:
        shown, total = int(m_first.group(1)), int(m_first.group(2))
        if shown < total:
            return True, f"showing first {shown} of {total}"

    # Check "showing lines X-Y of Z" only when Y < Z
    m_range = _SHOWING_RANGE_RE.search(text)
    if m_range:
        end_idx, total = int(m_range.group(2)), int(m_range.group(3))
        if end_idx < total:
            return True, f"showing up to {end_idx} of {total}"

    for pattern, reason in _TRUNCATION_PATTERNS:
        m = pattern.search(text)
        if m:
            snippet = m.group(0).strip()
            return True, snippet if len(snippet) <= 80 else reason

    return False, None


# --- Pagination detection and cursor tracking ---

_PAGE_OF_RE = re.compile(r"\bpage\s+(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)
_TEXT_CURSOR_RE = re.compile(
    r"\b(?:next[_ ]?cursor|next[_ ]?page[_ ]?token|continuation[_ ]?token)\s*[:=]\s*"
    r"['\"]?([A-Za-z0-9._:/+=-]{2,})['\"]?",
    re.IGNORECASE,
)
_TEXT_HAS_MORE_RE = re.compile(r"\b(?:has_more|hasMore|hasNextPage)\s*[:=]\s*true\b", re.IGNORECASE)


def _iter_metadata_dicts(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Return top-level dict and common 1-level pagination/metadata containers."""
    containers = [obj]
    for key in ("pageInfo", "page_info", "pagination", "meta", "metadata", "result", "data"):
        sub = obj.get(key)
        if isinstance(sub, dict):
            containers.append(sub)
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
    """Extract a PaginationEvent from a tool output if pagination markers are present."""
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
        for container in _iter_metadata_dicts(obj):
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
            if page < total_pages:
                has_more = True
        if next_cursor is not None and not has_more:
            # Presence of a non-empty nextCursor/nextPageToken implies more pages unless explicitly
            # overridden by hasNextPage=False on an endCursor.
            has_explicit_false = any(
                container.get(k) is False
                for container in _iter_metadata_dicts(obj)
                for k in ("has_more", "hasMore", "hasNextPage", "has_next_page")
            )
            if not has_explicit_false:
                has_more = True
    else:
        # Text-based pagination detection
        m_page = _PAGE_OF_RE.search(raw_output)
        if m_page:
            page = int(m_page.group(1))
            total_pages = int(m_page.group(2))
            found_signal = True
            if page < total_pages:
                has_more = True

        m_cur = _TEXT_CURSOR_RE.search(raw_output)
        if m_cur:
            candidate = m_cur.group(1).strip()
            if candidate.lower() not in ("null", "none", "false", "empty"):
                next_cursor = candidate
                has_more = True
                found_signal = True

        if _TEXT_HAS_MORE_RE.search(raw_output):
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
        resource=_infer_resource_name(tool_name, raw_input),
        page=page,
        total_pages=total_pages,
        has_more=has_more,
        next_cursor=next_cursor,
        cursor_followed=False,
        item_count=item_count,
    )


def _contains_string_value(data: Any, needle: str) -> bool:
    """Recursively check whether `needle` appears in any string value in `data`."""
    if isinstance(data, str):
        return needle == data or needle in data
    if isinstance(data, dict):
        return any(_contains_string_value(v, needle) for v in data.values())
    if isinstance(data, (list, tuple)):
        return any(_contains_string_value(item, needle) for item in data)
    return False


def mark_followed_cursors(
    events: list[ToolEvent],
    paginations: list[PaginationEvent],
) -> None:
    """Mark each PaginationEvent as cursor_followed if a later tool call fetches the next page."""
    for p in paginations:
        if p.cursor_followed:
            continue
        if not p.has_more and not p.next_cursor:
            continue
        for later in events:
            if later.event_index <= p.event_index:
                continue
            if p.next_cursor and _contains_string_value(later.raw_input, p.next_cursor):
                p.cursor_followed = True
                break
            if (
                p.page is not None
                and p.total_pages is not None
                and p.page < p.total_pages
                and later.tool_name == p.tool_name
            ):
                later_page = safe_int(
                    later.raw_input.get("page")
                    or later.raw_input.get("current_page")
                    or later.raw_input.get("currentPage")
                )
                if later_page is not None and later_page > p.page:
                    p.cursor_followed = True
                    break


# --- Shell command analysis ---


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


def _split_shell_segments(command: str, delimiters: tuple[str, ...]) -> list[str]:
    """Split a shell command on top-level delimiters, respecting quotes and escapes."""
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
        matched_delim = None
        for d in delimiters:
            if command.startswith(d, i):
                # Avoid splitting '||' when looking for '|'
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


def _safe_shlex_split(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


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


def _parse_matched_files_from_grep_output(output: str) -> tuple[list[str], int]:
    """Extract matched file paths and line match count from grep/rg output."""
    if not output.strip():
        return [], 0
    files: list[str] = []
    seen: set[str] = set()
    match_lines = 0
    for line in output.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        if _TRAILING_NOTICE_RE.search(s):
            continue
        match_lines += 1
        # Format `path/to/file.py:12:matched text` or `path/to/file.py`
        if ":" in s:
            prefix = s.split(":", 1)[0].strip()
            if prefix and not prefix.startswith(" ") and prefix not in seen:
                seen.add(prefix)
                files.append(prefix)
        elif " " not in s and s not in seen:
            seen.add(s)
            files.append(s)
    return files, match_lines


@dataclass
class ShellDerivedEvents:
    """Secondary typed events inferred from a shell command invocation."""

    file_reads: list[FileReadEvent] = field(default_factory=list)
    file_edits: list[FileEditEvent] = field(default_factory=list)
    searches: list[SearchEvent] = field(default_factory=list)


_REDIR_RE = re.compile(r"(?<![0-9&])>>?\s*([^\s;&|]+)")
_SED_RANGE_RE = re.compile(r"^(\d+)(?:,(\d+))?p$")


def analyze_shell_command(
    *,
    command: str,
    stdout: str,
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

    # Split compound commands (`&&`, `;`, `||`) while tracking `cd <dir>`
    compounds = _split_shell_segments(command, ("&&", "||", ";"))
    active_subdir: str | None = None

    for comp in compounds:
        pipe_segments = _split_shell_segments(comp, ("|",))
        if not pipe_segments:
            continue
        first_tokens = _safe_shlex_split(pipe_segments[0])
        if not first_tokens:
            continue

        # Handle `cd <dir>` prefix in compound commands
        if first_tokens[0] == "cd" and len(first_tokens) >= 2:
            active_subdir = first_tokens[1]
            continue

        # Check for redirection-based file modifications (even on chained commands)
        has_write_redirect = False
        if not is_error and exit_code in (None, 0):
            for m in _REDIR_RE.finditer(comp):
                target = m.group(1).strip("'\"")
                if target in ("/dev/null",) or target.startswith("&"):
                    continue
                has_write_redirect = True
                derived.file_edits.append(
                    FileEditEvent(
                        event_id=event_id,
                        event_index=event_index,
                        tool_name=tool_name,
                        timestamp=timestamp,
                        raw_input=raw_input,
                        raw_output=raw_output,
                        file_path=target,
                        operation="bash_modify",
                    )
                )

        cmd_name = Path(first_tokens[0]).name
        # Handle `git grep` and `git rm`
        subcmd = first_tokens[1] if len(first_tokens) > 1 else ""
        if cmd_name == "git" and subcmd == "grep":
            _analyze_search_tokens(
                derived=derived,
                search_cmd="git grep",
                tokens=first_tokens[2:],
                pipe_segments=pipe_segments,
                active_subdir=active_subdir,
                stdout=stdout,
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
                    derived.file_edits.append(
                        FileEditEvent(
                            event_id=event_id,
                            event_index=event_index,
                            tool_name=tool_name,
                            timestamp=timestamp,
                            raw_input=raw_input,
                            raw_output=raw_output,
                            file_path=tok,
                            operation="delete",
                        )
                    )
            continue

        if cmd_name in ("rg", "grep", "egrep", "fgrep", "ag", "ack"):
            _analyze_search_tokens(
                derived=derived,
                search_cmd=cmd_name,
                tokens=first_tokens[1:],
                pipe_segments=pipe_segments,
                active_subdir=active_subdir,
                stdout=stdout,
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
                pipe_segments=pipe_segments,
                active_subdir=active_subdir,
                stdout=stdout,
                event_id=event_id,
                event_index=event_index,
                tool_name=tool_name,
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
                is_error=is_error,
                truncated=truncated,
                truncation_reason=truncation_reason,
            )
            continue

        # Only parse single-command reads when no write redirection occurred and command succeeded
        if (
            not has_write_redirect
            and not is_error
            and exit_code in (None, 0)
            and len(compounds) <= 2
            and cmd_name in ("cat", "nl", "head", "tail", "sed")
        ):
            _analyze_read_tokens(
                derived=derived,
                cmd_name=cmd_name,
                tokens=first_tokens[1:],
                pipe_segments=pipe_segments,
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
                        derived.file_edits.append(
                            FileEditEvent(
                                event_id=event_id,
                                event_index=event_index,
                                tool_name=tool_name,
                                timestamp=timestamp,
                                raw_input=raw_input,
                                raw_output=raw_output,
                                file_path=tok,
                                operation="delete",
                            )
                        )
            elif cmd_name == "sed" and any(
                t == "-i" or t.startswith("-i") for t in first_tokens[1:]
            ):
                # Last non-flag argument(s) after expression are edited files
                positionals = [t for t in first_tokens[1:] if not t.startswith("-")]
                for target in positionals[1:]:
                    derived.file_edits.append(
                        FileEditEvent(
                            event_id=event_id,
                            event_index=event_index,
                            tool_name=tool_name,
                            timestamp=timestamp,
                            raw_input=raw_input,
                            raw_output=raw_output,
                            file_path=target,
                            operation="bash_modify",
                        )
                    )
            elif cmd_name in ("mv", "cp"):
                positionals = [t for t in first_tokens[1:] if not t.startswith("-")]
                if len(positionals) >= 2:
                    derived.file_edits.append(
                        FileEditEvent(
                            event_id=event_id,
                            event_index=event_index,
                            tool_name=tool_name,
                            timestamp=timestamp,
                            raw_input=raw_input,
                            raw_output=raw_output,
                            file_path=positionals[-1],
                            operation="bash_modify",
                        )
                    )
                    if cmd_name == "mv":
                        derived.file_edits.append(
                            FileEditEvent(
                                event_id=event_id,
                                event_index=event_index,
                                tool_name=tool_name,
                                timestamp=timestamp,
                                raw_input=raw_input,
                                raw_output=raw_output,
                                file_path=positionals[0],
                                operation="delete",
                            )
                        )

        # Check `| tee <file>` in pipes
        if not is_error and exit_code in (None, 0):
            for seg in pipe_segments[1:]:
                p_toks = _safe_shlex_split(seg)
                if p_toks and Path(p_toks[0]).name == "tee":
                    for tok in p_toks[1:]:
                        if not tok.startswith("-"):
                            derived.file_edits.append(
                                FileEditEvent(
                                    event_id=event_id,
                                    event_index=event_index,
                                    tool_name=tool_name,
                                    timestamp=timestamp,
                                    raw_input=raw_input,
                                    raw_output=raw_output,
                                    file_path=tok,
                                    operation="bash_modify",
                                )
                            )

    return derived


def _analyze_search_tokens(
    *,
    derived: ShellDerivedEvents,
    search_cmd: str,
    tokens: list[str],
    pipe_segments: list[str],
    active_subdir: str | None,
    stdout: str,
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
    query: str | None = None
    search_paths: list[str] = []
    glob_pattern: str | None = None
    excluded_dirs: list[str] = []
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
            if tok in ("-e", "--regexp") and i + 1 < len(tokens):
                query = tokens[i + 1]
                i += 2
                continue
            if tok in ("-g", "--glob", "--include") and i + 1 < len(tokens):
                val = tokens[i + 1]
                if val.startswith("!"):
                    excluded_dirs.append(val[1:].strip("/").removesuffix("/**"))
                else:
                    glob_pattern = val
                i += 2
                continue
            if tok.startswith("--glob=") or tok.startswith("--include="):
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
            if tok.startswith("--exclude-dir=") or tok.startswith("--exclude="):
                excluded_dirs.append(tok.split("=", 1)[1])
                i += 1
                continue
            if tok in ("-m", "--max-count") and i + 1 < len(tokens):
                val_int = safe_int(tokens[i + 1])
                if val_int is not None and val_int > 0 and result_limit is None:
                    result_limit = val_int
                i += 2
                continue
            if tok in ("-t", "--type", "-A", "-B", "-C", "-f", "--file") and i + 1 < len(tokens):
                i += 2
                continue
            i += 1
            continue

        if query is None:
            query = tok
        else:
            search_paths.append(tok)
        i += 1

    # Note: grep/rg exit code 1 means "no matches", not a tool crash
    effective_error = is_error and exit_code not in (0, 1, None)
    matched_files, match_count = _parse_matched_files_from_grep_output(stdout)
    is_trunc, trunc_reason = (truncated, truncation_reason)
    if not is_trunc:
        is_trunc, trunc_reason = detect_truncation(
            stdout,
            tool_name=search_cmd,
            result_limit=result_limit,
            observed_count=match_count,
        )

    search_path = search_paths[0] if search_paths else active_subdir
    derived.searches.append(
        SearchEvent(
            event_id=event_id,
            event_index=event_index,
            tool_name=f"{tool_name}:{search_cmd}",
            timestamp=timestamp,
            raw_input=raw_input,
            raw_output=raw_output,
            is_error=effective_error,
            truncated=is_trunc,
            truncation_reason=trunc_reason,
            query=query or "",
            search_path=search_path,
            glob_pattern=glob_pattern,
            excluded_dirs=excluded_dirs,
            match_count=match_count,
            matched_files=matched_files,
            output=stdout,
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
    stdout: str,
    event_id: str,
    event_index: int,
    tool_name: str,
    timestamp: str | None,
    raw_input: dict[str, Any],
    raw_output: str,
    is_error: bool,
    truncated: bool,
    truncation_reason: str | None,
) -> None:
    search_path: str | None = None
    glob_pattern: str | None = None
    excluded_dirs: list[str] = []
    result_limit = _extract_head_limit(pipe_segments)

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-name", "-iname") and i + 1 < len(tokens):
            glob_pattern = tokens[i + 1]
            i += 2
            continue
        if tok in ("-E", "--exclude") and i + 1 < len(tokens):
            excluded_dirs.append(tokens[i + 1])
            i += 2
            continue
        if not tok.startswith("-") and tok not in ("!", "(", ")") and search_path is None:
            if search_cmd == "find":
                search_path = tok
            else:
                glob_pattern = glob_pattern or tok
        elif search_cmd == "fd" and not tok.startswith("-") and search_path is None:
            search_path = tok
        i += 1

    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    is_trunc, trunc_reason = (truncated, truncation_reason)
    if not is_trunc:
        is_trunc, trunc_reason = detect_truncation(
            stdout,
            tool_name=search_cmd,
            result_limit=result_limit,
            observed_count=len(lines),
        )

    derived.searches.append(
        SearchEvent(
            event_id=event_id,
            event_index=event_index,
            tool_name=f"{tool_name}:{search_cmd}",
            timestamp=timestamp,
            raw_input=raw_input,
            raw_output=raw_output,
            is_error=is_error,
            truncated=is_trunc,
            truncation_reason=trunc_reason,
            query=glob_pattern or "",
            search_path=search_path or active_subdir,
            glob_pattern=glob_pattern,
            excluded_dirs=excluded_dirs,
            match_count=len(lines),
            matched_files=lines,
            output=stdout,
            result_limit=result_limit,
        )
    )


def _analyze_read_tokens(
    *,
    derived: ShellDerivedEvents,
    cmd_name: str,
    tokens: list[str],
    pipe_segments: list[str],
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
    if cmd_name == "sed":
        # Only recognize `sed -n 'X,Yp' <file>` (read-only print range)
        if "-n" not in tokens or any(t == "-i" or t.startswith("-i") for t in tokens):
            return
        positionals = [t for t in tokens if not t.startswith("-")]
        if len(positionals) != 2:
            return
        expr, file_path = positionals
        m = _SED_RANGE_RE.match(expr.strip())
        if not m:
            return
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        parsed = parse_numbered_lines(stdout, offset=start, limit=end - start + 1)
        derived.file_reads.append(
            FileReadEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=f"{tool_name}:sed",
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
                truncated=truncated or parsed.truncated,
                truncation_reason=truncation_reason or parsed.truncation_reason,
                file_path=file_path,
                start_line=start,
                end_line=min(end, parsed.end_line) if parsed.end_line else end,
                offset=start,
                limit=end - start + 1,
                content=parsed.content,
                line_count=parsed.line_count,
            )
        )
        return

    # `cat`, `nl`, `head`, `tail`
    limit: int | None = None
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
        if tok.startswith("-"):
            i += 1
            continue
        files.append(tok)
        i += 1

    if len(files) != 1:
        return
    file_path = files[0]
    if cmd_name == "head" and limit is None:
        limit = 10
    if cmd_name == "tail":
        # Without knowing total file lines, tail cannot guarantee start_line=1
        parsed = parse_numbered_lines(stdout)
        derived.file_reads.append(
            FileReadEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=f"{tool_name}:tail",
                timestamp=timestamp,
                raw_input=raw_input,
                raw_output=raw_output,
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

    pipe_limit = _extract_head_limit(pipe_segments)
    eff_limit = limit if limit is not None else pipe_limit
    parsed = parse_numbered_lines(stdout, offset=1, limit=eff_limit)
    derived.file_reads.append(
        FileReadEvent(
            event_id=event_id,
            event_index=event_index,
            tool_name=f"{tool_name}:{cmd_name}",
            timestamp=timestamp,
            raw_input=raw_input,
            raw_output=raw_output,
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

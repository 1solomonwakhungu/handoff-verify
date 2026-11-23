"""Parser for Claude Code JSONL transcripts (main sessions and subagent logs)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from handoff_verify.models import (
    AssistantMessage,
    CommandEvent,
    FileEditEvent,
    FileReadEvent,
    ParsedTranscript,
    SearchEvent,
    ToolEvent,
)
from handoff_verify.parsers._common import (
    analyze_shell_command,
    detect_pagination,
    detect_truncation,
    extract_text_content,
    is_truncation_banner_line,
    load_jsonl,
    mark_followed_cursors,
    parse_numbered_lines,
    safe_int,
    safe_str,
    split_lf_lines,
)

_LEADING_EXIT_CODE_RE = re.compile(r"^Exit code[:\s]+(-?\d+)\s*(?:\r?\n)?", re.IGNORECASE)
_TRAILING_EXIT_CODE_RE = re.compile(r"(?:^|\r?\n)Exit code[:\s]+(-?\d+)\s*$", re.IGNORECASE)
_PAGINATION_BANNER_RE = re.compile(r"^\[showing\s+results\s+with\s+pagination\b", re.IGNORECASE)
_NO_MATCH_SENTINELS = frozenset(
    {
        "no files found",
        "no matches found",
        "no matching files found",
        "0 matches found",
    }
)


@dataclass
class _PendingToolCall:
    event_index: int
    event_id: str
    tool_name: str
    raw_input: dict[str, Any]
    timestamp: str | None
    raw_output: str = ""
    is_error: bool = False
    tool_use_result: Any = None
    has_result: bool = False


def _extract_negative_globs(raw_input: dict[str, Any]) -> list[str]:
    excluded: list[str] = []
    glob_val = raw_input.get("glob")
    if isinstance(glob_val, str) and glob_val.startswith("!"):
        excluded.append(glob_val[1:].strip("/").removesuffix("/**"))
    for key in ("exclude", "excluded_dirs", "ignore"):
        val = raw_input.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.strip():
                    excluded.append(item.strip())
        elif isinstance(val, str) and val.strip():
            excluded.append(val.strip())
    return excluded


def _parse_grep_output(
    raw_output: str,
    *,
    output_mode: str,
    tool_use_result: Any,
) -> tuple[list[str], int]:
    """Parse matched file paths and match count from Claude Code Grep output."""
    if isinstance(tool_use_result, dict):
        filenames = tool_use_result.get("filenames")
        if isinstance(filenames, list) and all(isinstance(f, str) for f in filenames):
            num_files = safe_int(tool_use_result.get("numFiles"))
            if output_mode == "files_with_matches":
                return list(filenames), num_files if num_files is not None else len(filenames)

    stripped = raw_output.strip()
    if not stripped or stripped.lower() in _NO_MATCH_SENTINELS:
        return [], 0

    lines = [line.strip() for line in split_lf_lines(stripped) if line.strip()]
    matched_files: list[str] = []
    seen: set[str] = set()
    match_count = 0

    for line in lines:
        low = line.lower()
        if low in _NO_MATCH_SENTINELS:
            continue
        if is_truncation_banner_line(line)[0] or _PAGINATION_BANNER_RE.match(line):
            continue
        if low.startswith(("found ", "showing ")):
            continue
        if output_mode == "count" and ":" in line:
            path_part, count_part = line.rsplit(":", 1)
            cnt = safe_int(count_part)
            if cnt is not None:
                match_count += cnt
                if path_part and path_part not in seen:
                    seen.add(path_part)
                    matched_files.append(path_part)
                continue
        if output_mode == "content":
            match_count += 1
            if ":" in line:
                first = line.split(":", 1)[0].strip()
                if first and not first.isdigit() and first not in seen:
                    seen.add(first)
                    matched_files.append(first)
            continue
        # files_with_matches (default)
        if line not in seen:
            seen.add(line)
            matched_files.append(line)
        match_count += 1

    return matched_files, match_count


def _parse_glob_output(raw_output: str, tool_use_result: Any) -> tuple[list[str], int]:
    if isinstance(tool_use_result, dict):
        filenames = tool_use_result.get("filenames")
        if isinstance(filenames, list) and all(isinstance(f, str) for f in filenames):
            num_files = safe_int(tool_use_result.get("numFiles"))
            return list(filenames), num_files if num_files is not None else len(filenames)

    stripped = raw_output.strip()
    if not stripped or stripped.lower() in _NO_MATCH_SENTINELS:
        return [], 0

    files: list[str] = []
    for line in split_lf_lines(stripped):
        s = line.strip()
        if not s or s.lower() in _NO_MATCH_SENTINELS:
            continue
        if is_truncation_banner_line(s)[0]:
            continue
        if s.startswith("..."):
            continue
        files.append(s)
    return files, len(files)


def parse_claude_code(
    source: str | Path | Iterable[str],
    *,
    transcript_path: str | None = None,
) -> ParsedTranscript:
    """Parse a Claude Code JSONL transcript into a normalized ParsedTranscript."""
    loaded, detected_path = load_jsonl(source)
    transcript = ParsedTranscript(
        source_format="claude_code",
        transcript_path=transcript_path or detected_path,
        malformed_lines=loaded.malformed_lines,
        parse_warnings=list(loaded.parse_warnings),
    )

    pending_list: list[_PendingToolCall] = []
    pending_by_id: dict[str, _PendingToolCall] = {}
    last_assistant_msg_id: str | None = None
    seen_text_blocks_by_msg_id: dict[str, set[str]] = {}

    for line_no, record in loaded.records:
        if transcript.cwd is None and isinstance(record.get("cwd"), str) and record["cwd"].strip():
            transcript.cwd = record["cwd"].strip()

        rec_type = record.get("type")
        # Strictly process only `assistant` and `user` records; ignore side-channel blobs
        # such as `api-request-blob`, `progress`, `system`, `summary`, `file-history-snapshot`.
        if rec_type not in ("assistant", "user"):
            continue

        timestamp = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        message = record.get("message")
        tool_use_result = record.get("toolUseResult")

        if not isinstance(message, dict):
            continue

        content = message.get("content")
        msg_id = message.get("id") if isinstance(message.get("id"), str) else None

        if rec_type == "assistant":
            text_parts: list[str] = []
            if isinstance(content, str):
                if content.strip():
                    text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text" and isinstance(block.get("text"), str):
                        if block["text"].strip():
                            text_parts.append(block["text"])
                    elif btype == "tool_use":
                        tool_id = safe_str(block.get("id")) or f"tool_{len(pending_list)}"
                        if tool_id in pending_by_id:
                            continue
                        tool_name = safe_str(block.get("name"))
                        raw_input = block.get("input")
                        if not isinstance(raw_input, dict):
                            raw_input = {"value": raw_input} if raw_input is not None else {}
                        call = _PendingToolCall(
                            event_index=len(pending_list),
                            event_id=tool_id,
                            tool_name=tool_name,
                            raw_input=raw_input,
                            timestamp=timestamp,
                        )
                        pending_list.append(call)
                        pending_by_id[tool_id] = call

            if text_parts:
                after_idx = len(pending_list) - 1 if pending_list else None
                if msg_id is not None:
                    seen_blocks = seen_text_blocks_by_msg_id.setdefault(msg_id, set())
                    unique_parts = [p for p in text_parts if p not in seen_blocks]
                    seen_blocks.update(unique_parts)
                else:
                    unique_parts = text_parts

                if unique_parts:
                    combined_text = "\n".join(unique_parts)
                    if (
                        msg_id is not None
                        and msg_id == last_assistant_msg_id
                        and transcript.assistant_messages
                    ):
                        prev = transcript.assistant_messages[-1]
                        prev.text = f"{prev.text}\n{combined_text}"
                        prev.after_event_index = after_idx
                    else:
                        transcript.assistant_messages.append(
                            AssistantMessage(
                                index=len(transcript.assistant_messages),
                                text=combined_text,
                                timestamp=timestamp,
                                after_event_index=after_idx,
                            )
                        )
                        last_assistant_msg_id = msg_id

        elif rec_type == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = safe_str(block.get("tool_use_id"))
                call = pending_by_id.get(tool_use_id)
                if call is None:
                    transcript.parse_warnings.append(
                        f"Line {line_no}: orphan tool_result for tool_use_id={tool_use_id!r}"
                    )
                    continue
                call.raw_output = extract_text_content(block.get("content"))
                call.is_error = bool(block.get("is_error", False))
                call.tool_use_result = tool_use_result
                call.has_result = True

    # Convert pending calls in chronological order into typed events
    for call in pending_list:
        _materialize_claude_event(transcript, call)

    # Mark pagination cursors that were followed by later same-tool calls
    mark_followed_cursors(transcript.events, transcript.paginations)

    # Mark final assistant message
    if transcript.assistant_messages:
        transcript.assistant_messages[-1].is_final = True
        transcript.final_text = transcript.assistant_messages[-1].text

    return transcript


def _materialize_claude_event(transcript: ParsedTranscript, call: _PendingToolCall) -> None:
    name = call.tool_name
    raw_in = call.raw_input
    raw_out = call.raw_output
    tur = call.tool_use_result if isinstance(call.tool_use_result, dict) else {}

    # Unresolved calls (no matching tool_result) stay in `events` with has_result=False
    # and never enter typed evidence lists.
    if not call.has_result:
        transcript.events.append(
            ToolEvent(
                event_id=call.event_id,
                event_index=call.event_index,
                tool_name=name,
                timestamp=call.timestamp,
                raw_input=raw_in,
                raw_output="",
                is_error=False,
                has_result=False,
            )
        )
        return

    if name == "Read":
        file_path = safe_str(raw_in.get("file_path") or raw_in.get("path"))
        offset = safe_int(raw_in.get("offset"))
        limit = safe_int(raw_in.get("limit"))

        if call.is_error:
            transcript.events.append(
                ToolEvent(
                    event_id=call.event_id,
                    event_index=call.event_index,
                    tool_name=name,
                    timestamp=call.timestamp,
                    raw_input=raw_in,
                    raw_output=raw_out,
                    is_error=True,
                    has_result=True,
                )
            )
            return

        parsed_read = parse_numbered_lines(
            raw_out, offset=offset, limit=limit, expect_line_numbers=True
        )
        truncated = parsed_read.truncated
        trunc_reason = parsed_read.truncation_reason

        # Enrich with Claude Code's optional toolUseResult.file metadata if present
        file_meta = tur.get("file") if isinstance(tur.get("file"), dict) else {}
        if file_meta:
            meta_start = safe_int(file_meta.get("startLine"))
            meta_num = safe_int(file_meta.get("numLines"))
            meta_total = safe_int(file_meta.get("totalLines"))
            if parsed_read.start_line is None and meta_start is not None:
                parsed_read.start_line = meta_start
            if parsed_read.line_count == 0 and meta_num is not None:
                parsed_read.line_count = meta_num
            if (
                parsed_read.end_line is None
                and parsed_read.start_line is not None
                and parsed_read.line_count > 0
            ):
                parsed_read.end_line = parsed_read.start_line + parsed_read.line_count - 1
            if (
                not truncated
                and meta_total is not None
                and parsed_read.end_line is not None
                and parsed_read.end_line < meta_total
                and limit is None
            ):
                truncated = True
                trunc_reason = f"showing up to line {parsed_read.end_line} of {meta_total}"
        if tur.get("truncated") is True and not truncated:
            truncated = True
            trunc_reason = "toolUseResult.truncated=true"

        ev = FileReadEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=raw_out,
            is_error=False,
            has_result=True,
            truncated=truncated,
            truncation_reason=trunc_reason,
            file_path=file_path,
            start_line=parsed_read.start_line,
            end_line=parsed_read.end_line,
            offset=offset,
            limit=limit,
            content=parsed_read.content,
            line_count=parsed_read.line_count,
        )
        transcript.events.append(ev)
        transcript.file_reads.append(ev)
        return

    if name == "Grep":
        if call.is_error:
            transcript.events.append(
                ToolEvent(
                    event_id=call.event_id,
                    event_index=call.event_index,
                    tool_name=name,
                    timestamp=call.timestamp,
                    raw_input=raw_in,
                    raw_output=raw_out,
                    is_error=True,
                    has_result=True,
                )
            )
            return

        output_mode = safe_str(raw_in.get("output_mode")) or "files_with_matches"
        head_limit = safe_int(raw_in.get("head_limit")) or safe_int(tur.get("appliedLimit"))
        matched_files, match_count = _parse_grep_output(
            raw_out, output_mode=output_mode, tool_use_result=tur
        )
        observed_count = len(matched_files) if output_mode == "files_with_matches" else match_count
        truncated, trunc_reason = detect_truncation(
            raw_out,
            tool_name=name,
            result_limit=head_limit,
            observed_count=observed_count,
        )
        if tur.get("truncated") is True and not truncated:
            truncated = True
            trunc_reason = "toolUseResult.truncated=true"

        sp = safe_str(raw_in.get("path")) or None
        ev_search = SearchEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=raw_out,
            is_error=False,
            has_result=True,
            truncated=truncated,
            truncation_reason=trunc_reason,
            query=safe_str(raw_in.get("pattern")),
            search_path=sp,
            search_paths=[sp] if sp else [],
            glob_pattern=safe_str(raw_in.get("glob") or raw_in.get("type")) or None,
            excluded_dirs=_extract_negative_globs(raw_in),
            match_count=match_count,
            matched_files=matched_files,
            output=raw_out,
            result_limit=head_limit,
        )
        transcript.events.append(ev_search)
        transcript.searches.append(ev_search)
        return

    if name in ("Glob", "LS"):
        if call.is_error:
            transcript.events.append(
                ToolEvent(
                    event_id=call.event_id,
                    event_index=call.event_index,
                    tool_name=name,
                    timestamp=call.timestamp,
                    raw_input=raw_in,
                    raw_output=raw_out,
                    is_error=True,
                    has_result=True,
                )
            )
            return

        matched_files, match_count = _parse_glob_output(raw_out, tur)
        result_limit = safe_int(raw_in.get("limit"))
        truncated, trunc_reason = detect_truncation(
            raw_out,
            tool_name=name,
            result_limit=result_limit,
            observed_count=match_count,
        )
        if tur.get("truncated") is True and not truncated:
            truncated = True
            trunc_reason = "toolUseResult.truncated=true"

        sp = safe_str(raw_in.get("path")) or None
        ev_glob = SearchEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=raw_out,
            is_error=False,
            has_result=True,
            truncated=truncated,
            truncation_reason=trunc_reason,
            query=safe_str(raw_in.get("pattern")),
            search_path=sp,
            search_paths=[sp] if sp else [],
            glob_pattern=safe_str(raw_in.get("pattern")) or None,
            excluded_dirs=_extract_negative_globs(raw_in),
            match_count=match_count,
            matched_files=matched_files,
            output=raw_out,
            result_limit=result_limit,
        )
        transcript.events.append(ev_glob)
        transcript.searches.append(ev_glob)
        return

    if name == "Bash":
        command = safe_str(raw_in.get("command"))
        if isinstance(tur.get("stdout"), str):
            stdout = tur["stdout"]
        else:
            stdout = _LEADING_EXIT_CODE_RE.sub("", raw_out, count=1)
        stderr = tur["stderr"] if isinstance(tur.get("stderr"), str) else ""
        exit_code = safe_int(tur.get("exitCode") or tur.get("exit_code"))
        if exit_code is None:
            m_lead = _LEADING_EXIT_CODE_RE.match(raw_out)
            m_trail = _TRAILING_EXIT_CODE_RE.search(raw_out) if not m_lead else None
            if m_lead:
                exit_code = int(m_lead.group(1))
            elif m_trail:
                exit_code = int(m_trail.group(1))
            elif not call.is_error:
                exit_code = 0
            # Note: when call.is_error is True without an explicit Exit code header
            # (e.g. user rejection or timeout), leave exit_code=None rather than
            # synthesizing exit_code=1 (which grep/rg uses for "no matches").

        truncated, trunc_reason = detect_truncation(stdout, tool_name=name)
        if tur.get("truncated") is True and not truncated:
            truncated = True
            trunc_reason = "toolUseResult.truncated=true"

        ev_cmd = CommandEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=raw_out,
            is_error=call.is_error,
            has_result=True,
            truncated=truncated,
            truncation_reason=trunc_reason,
            command=command,
            cwd=transcript.cwd,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )
        transcript.events.append(ev_cmd)
        transcript.commands.append(ev_cmd)

        derived = analyze_shell_command(
            command=command,
            stdout=stdout,
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=raw_out,
            is_error=call.is_error,
            exit_code=exit_code,
            truncated=truncated,
            truncation_reason=trunc_reason,
        )
        transcript.file_reads.extend(derived.file_reads)
        transcript.file_edits.extend(derived.file_edits)
        transcript.searches.extend(derived.searches)

        # Skip pagination heuristics on shell commands that read or searched repo files
        if not derived.file_reads and not derived.searches:
            pag = detect_pagination(
                event_id=call.event_id,
                event_index=call.event_index,
                tool_name=name,
                timestamp=call.timestamp,
                raw_input=raw_in,
                raw_output=stdout,
                is_error=call.is_error,
            )
            if pag is not None:
                transcript.paginations.append(pag)
        return

    if name == "Edit":
        ev_edit = FileEditEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=raw_out,
            is_error=call.is_error,
            has_result=True,
            file_path=safe_str(raw_in.get("file_path") or raw_in.get("path")),
            operation="edit",
            old_text=raw_in.get("old_string")
            if isinstance(raw_in.get("old_string"), str)
            else None,
            new_text=raw_in.get("new_string")
            if isinstance(raw_in.get("new_string"), str)
            else None,
        )
        transcript.events.append(ev_edit)
        if not call.is_error:
            transcript.file_edits.append(ev_edit)
        return

    if name == "MultiEdit":
        file_path = safe_str(raw_in.get("file_path") or raw_in.get("path"))
        edits = raw_in.get("edits") if isinstance(raw_in.get("edits"), list) else []
        if not edits:
            ev_edit = FileEditEvent(
                event_id=call.event_id,
                event_index=call.event_index,
                tool_name=name,
                timestamp=call.timestamp,
                raw_input=raw_in,
                raw_output=raw_out,
                is_error=call.is_error,
                has_result=True,
                file_path=file_path,
                operation="edit",
            )
            transcript.events.append(ev_edit)
            if not call.is_error:
                transcript.file_edits.append(ev_edit)
            return

        first_event: FileEditEvent | None = None
        for entry in edits:
            if not isinstance(entry, dict):
                continue
            ev_edit = FileEditEvent(
                event_id=call.event_id,
                event_index=call.event_index,
                tool_name=name,
                timestamp=call.timestamp,
                raw_input=raw_in,
                raw_output=raw_out,
                is_error=call.is_error,
                has_result=True,
                file_path=file_path,
                operation="edit",
                old_text=entry.get("old_string")
                if isinstance(entry.get("old_string"), str)
                else None,
                new_text=entry.get("new_string")
                if isinstance(entry.get("new_string"), str)
                else None,
            )
            if first_event is None:
                first_event = ev_edit
            if not call.is_error:
                transcript.file_edits.append(ev_edit)
        if first_event is not None:
            transcript.events.append(first_event)
        return

    if name == "Write":
        content_val = raw_in.get("content") if isinstance(raw_in.get("content"), str) else None
        ev_write = FileEditEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=raw_out,
            is_error=call.is_error,
            has_result=True,
            file_path=safe_str(raw_in.get("file_path") or raw_in.get("path")),
            operation="write",
            content=content_val,
        )
        transcript.events.append(ev_write)
        if not call.is_error:
            transcript.file_edits.append(ev_write)
        return

    if name == "NotebookEdit":
        edit_mode = safe_str(raw_in.get("edit_mode")).lower()
        op = "delete" if edit_mode == "delete" else "edit"
        new_source = raw_in.get("new_source") if isinstance(raw_in.get("new_source"), str) else None
        ev_nb = FileEditEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=raw_out,
            is_error=call.is_error,
            has_result=True,
            file_path=safe_str(raw_in.get("notebook_path") or raw_in.get("file_path")),
            operation=op,
            new_text=new_source,
        )
        transcript.events.append(ev_nb)
        if not call.is_error:
            transcript.file_edits.append(ev_nb)
        return

    # WebFetch, MCP tools (`mcp__*`), and any other custom/harness tools
    truncated, trunc_reason = detect_truncation(raw_out, tool_name=name)
    ev_generic = ToolEvent(
        event_id=call.event_id,
        event_index=call.event_index,
        tool_name=name,
        timestamp=call.timestamp,
        raw_input=raw_in,
        raw_output=raw_out,
        is_error=call.is_error,
        has_result=True,
        truncated=truncated,
        truncation_reason=trunc_reason,
    )
    transcript.events.append(ev_generic)

    pag = detect_pagination(
        event_id=call.event_id,
        event_index=call.event_index,
        tool_name=name,
        timestamp=call.timestamp,
        raw_input=raw_in,
        raw_output=raw_out,
        is_error=call.is_error,
    )
    if pag is not None:
        pag.truncated = truncated
        pag.truncation_reason = trunc_reason
        transcript.paginations.append(pag)

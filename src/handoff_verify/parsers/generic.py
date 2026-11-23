"""Parser for the documented generic JSONL transcript schema.

Supported record `type` values:
- `meta`: `{"type": "meta", "cwd": "/path/to/repo"}`
- `file_read`: `{"type": "file_read", "file_path": "...", "start_line": 1, "end_line": 50,
  "offset": 1, "limit": 50, "content": "...", "truncated": false}`
- `file_edit`: `{"type": "file_edit", "file_path": "...", "operation": "edit|write|delete",
  "old_text": "...", "new_text": "...", "content": "..."}`
- `search`: `{"type": "search", "tool_name": "grep", "query": "...", "search_path": "src/",
  "search_paths": ["src/"], "glob_pattern": "*.py", "excluded_dirs": ["vendor"],
  "match_count": 0, "matched_files": [], "output": "...", "truncated": false, "result_limit": 50}`
- `command`: `{"type": "command", "command": "...", "cwd": "...", "stdout": "...",
  "stderr": "...", "exit_code": 0, "truncated": false}`
- `pagination`: `{"type": "pagination", "tool_name": "...", "resource": "...", "page": 1,
  "total_pages": 3, "has_more": true, "next_cursor": "cur_2", "item_count": 20}`
- `tool_call` / `tool_result`: generic paired tool calls
  `{"type": "tool_call", "id": "t1", "tool_name": "...", "input": {...}}`
  `{"type": "tool_result", "tool_call_id": "t1", "output": "...", "is_error": false}`
- `assistant`: `{"type": "assistant", "text": "...", "timestamp": "..."}`
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from handoff_verify.models import (
    AssistantMessage,
    CommandEvent,
    FileEditEvent,
    FileReadEvent,
    PaginationEvent,
    ParsedTranscript,
    SearchEvent,
    ToolEvent,
)
from handoff_verify.parsers._common import (
    MAX_PARSE_WARNINGS,
    analyze_shell_command,
    detect_pagination,
    detect_truncation,
    extract_text_content,
    load_jsonl,
    mark_followed_cursors,
    parse_numbered_lines,
    safe_int,
    safe_str,
)


def parse_generic(
    source: str | Path | Iterable[str],
    *,
    transcript_path: str | None = None,
    source_format: str = "generic",
) -> ParsedTranscript:
    """Parse a generic JSONL transcript into a normalized ParsedTranscript."""
    loaded, detected_path = load_jsonl(source)
    transcript = ParsedTranscript(
        source_format=source_format,
        transcript_path=transcript_path or detected_path,
        malformed_lines=loaded.malformed_lines,
        parse_warnings=list(loaded.parse_warnings),
    )

    pending_calls: dict[str, ToolEvent] = {}
    unrecognized_warnings = 0
    suppressed_unrecognized = 0

    for line_no, record in loaded.records:
        if transcript.cwd is None and isinstance(record.get("cwd"), str) and record["cwd"].strip():
            transcript.cwd = record["cwd"].strip()

        rec_type = safe_str(record.get("type")).lower()
        timestamp = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        event_index = len(transcript.events)
        event_id = safe_str(record.get("id") or record.get("event_id")) or f"ev_{event_index}"

        if rec_type == "meta":
            continue

        if rec_type == "assistant":
            text = extract_text_content(record.get("text") or record.get("content")).strip()
            if text:
                after_idx = len(transcript.events) - 1 if transcript.events else None
                transcript.assistant_messages.append(
                    AssistantMessage(
                        index=len(transcript.assistant_messages),
                        text=text,
                        timestamp=timestamp,
                        after_event_index=after_idx,
                    )
                )
            continue

        if rec_type == "file_read":
            raw_content = extract_text_content(record.get("content"))
            offset = safe_int(record.get("offset"))
            limit = safe_int(record.get("limit"))
            start_line = safe_int(record.get("start_line"))
            end_line = safe_int(record.get("end_line"))
            line_count = safe_int(record.get("line_count"))
            parsed_read = parse_numbered_lines(
                raw_content,
                offset=offset or start_line,
                limit=limit,
                expect_line_numbers=True,
            )

            eff_content = parsed_read.content if raw_content else ""
            if start_line is None:
                start_line = parsed_read.start_line
            if line_count is None:
                line_count = parsed_read.line_count
            if end_line is None:
                if start_line is not None and line_count > 0:
                    end_line = start_line + line_count - 1
                else:
                    end_line = parsed_read.end_line

            truncated = bool(record.get("truncated", False)) or parsed_read.truncated
            trunc_reason = (
                safe_str(record.get("truncation_reason")) or parsed_read.truncation_reason or None
            )
            is_error = bool(record.get("is_error", False))
            has_result = bool(record.get("has_result", True))
            ev_read = FileReadEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=safe_str(record.get("tool_name")) or "file_read",
                timestamp=timestamp,
                raw_input=dict(record),
                raw_output=raw_content,
                is_error=is_error,
                has_result=has_result,
                truncated=truncated,
                truncation_reason=trunc_reason,
                file_path=safe_str(record.get("file_path") or record.get("path")),
                start_line=start_line,
                end_line=end_line,
                offset=offset,
                limit=limit,
                content=eff_content,
                line_count=line_count,
            )
            transcript.events.append(ev_read)
            if has_result and not is_error:
                transcript.file_reads.append(ev_read)
            continue

        if rec_type == "file_edit":
            is_error = bool(record.get("is_error", False))
            has_result = bool(record.get("has_result", True))
            ev_edit = FileEditEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=safe_str(record.get("tool_name")) or "file_edit",
                timestamp=timestamp,
                raw_input=dict(record),
                raw_output=extract_text_content(record.get("output")),
                is_error=is_error,
                has_result=has_result,
                file_path=safe_str(record.get("file_path") or record.get("path")),
                operation=safe_str(record.get("operation")) or "edit",
                old_text=record.get("old_text")
                if isinstance(record.get("old_text"), str)
                else None,
                new_text=record.get("new_text")
                if isinstance(record.get("new_text"), str)
                else None,
                content=record.get("content") if isinstance(record.get("content"), str) else None,
            )
            transcript.events.append(ev_edit)
            if has_result and not is_error:
                transcript.file_edits.append(ev_edit)
            continue

        if rec_type == "search":
            output = extract_text_content(record.get("output"))
            result_limit = safe_int(record.get("result_limit") or record.get("limit"))
            raw_matched = record.get("matched_files")
            matched_files = (
                [safe_str(f) for f in raw_matched if safe_str(f)]
                if isinstance(raw_matched, list)
                else []
            )
            match_count = safe_int(record.get("match_count"))
            if match_count is None and matched_files:
                match_count = len(matched_files)

            raw_excluded = record.get("excluded_dirs")
            excluded_dirs = (
                [safe_str(d) for d in raw_excluded if safe_str(d)]
                if isinstance(raw_excluded, list)
                else []
            )

            raw_sp_list = record.get("search_paths")
            search_paths = (
                [safe_str(p) for p in raw_sp_list if safe_str(p)]
                if isinstance(raw_sp_list, list)
                else []
            )
            sp = safe_str(record.get("search_path") or record.get("path")) or None
            if not search_paths and sp:
                search_paths = [sp]
            elif search_paths and sp is None:
                sp = search_paths[0]

            truncated = bool(record.get("truncated", False))
            trunc_reason = safe_str(record.get("truncation_reason")) or None
            if not truncated:
                det_trunc, det_reason = detect_truncation(
                    output,
                    tool_name=safe_str(record.get("tool_name")) or "search",
                    result_limit=result_limit,
                    observed_count=match_count,
                )
                truncated = det_trunc
                trunc_reason = trunc_reason or det_reason

            is_error = bool(record.get("is_error", False))
            has_result = bool(record.get("has_result", True))
            ev_search = SearchEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=safe_str(record.get("tool_name")) or "search",
                timestamp=timestamp,
                raw_input=dict(record),
                raw_output=output,
                is_error=is_error,
                has_result=has_result,
                truncated=truncated,
                truncation_reason=trunc_reason,
                query=safe_str(record.get("query") or record.get("pattern")),
                search_path=sp,
                search_paths=search_paths,
                glob_pattern=safe_str(record.get("glob_pattern") or record.get("glob")) or None,
                excluded_dirs=excluded_dirs,
                match_count=match_count,
                matched_files=matched_files,
                output=output,
                result_limit=result_limit,
            )
            transcript.events.append(ev_search)
            if has_result and not is_error:
                transcript.searches.append(ev_search)
            continue

        if rec_type == "command":
            command = safe_str(record.get("command"))
            stdout = extract_text_content(record.get("stdout") or record.get("output"))
            stderr = extract_text_content(record.get("stderr"))
            exit_code = safe_int(record.get("exit_code"))
            is_error = bool(record.get("is_error", False)) or (
                exit_code is not None and exit_code != 0
            )
            has_result = bool(record.get("has_result", True))
            truncated = bool(record.get("truncated", False))
            trunc_reason = safe_str(record.get("truncation_reason")) or None
            if not truncated:
                det_trunc, det_reason = detect_truncation(stdout, tool_name="command")
                truncated = det_trunc
                trunc_reason = trunc_reason or det_reason

            ev_cmd = CommandEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=safe_str(record.get("tool_name")) or "command",
                timestamp=timestamp,
                raw_input=dict(record),
                raw_output=stdout,
                is_error=is_error,
                has_result=has_result,
                truncated=truncated,
                truncation_reason=trunc_reason,
                command=command,
                cwd=safe_str(record.get("cwd")) or transcript.cwd,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
            )
            transcript.events.append(ev_cmd)
            if has_result:
                transcript.commands.append(ev_cmd)
                derived = analyze_shell_command(
                    command=command,
                    stdout=stdout,
                    event_id=event_id,
                    event_index=event_index,
                    tool_name=ev_cmd.tool_name,
                    timestamp=timestamp,
                    raw_input=ev_cmd.raw_input,
                    raw_output=stdout,
                    is_error=is_error,
                    exit_code=exit_code,
                    truncated=truncated,
                    truncation_reason=trunc_reason,
                )
                transcript.file_reads.extend(derived.file_reads)
                transcript.file_edits.extend(derived.file_edits)
                transcript.searches.extend(derived.searches)
            continue

        if rec_type == "pagination":
            page = safe_int(record.get("page"))
            total_pages = safe_int(record.get("total_pages"))
            next_cursor = safe_str(record.get("next_cursor") or record.get("nextCursor")) or None
            if "has_more" in record:
                has_more = bool(record.get("has_more"))
            else:
                has_more = False
                if (
                    page is not None
                    and total_pages is not None
                    and page < total_pages
                    or next_cursor is not None
                ):
                    has_more = True

            is_error = bool(record.get("is_error", False))
            has_result = bool(record.get("has_result", True))
            raw_in = record.get("input") if isinstance(record.get("input"), dict) else dict(record)
            ev_pag = PaginationEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=safe_str(record.get("tool_name")) or "pagination",
                timestamp=timestamp,
                raw_input=raw_in,
                raw_output=extract_text_content(record.get("output")),
                is_error=is_error,
                has_result=has_result,
                truncated=bool(record.get("truncated", False)),
                truncation_reason=safe_str(record.get("truncation_reason")) or None,
                resource=safe_str(record.get("resource") or record.get("tool_name")),
                page=page,
                total_pages=total_pages,
                has_more=has_more,
                next_cursor=next_cursor,
                cursor_followed=bool(record.get("cursor_followed", False)),
                item_count=safe_int(record.get("item_count")),
            )
            transcript.events.append(ev_pag)
            if has_result and not is_error:
                transcript.paginations.append(ev_pag)
            continue

        if rec_type == "tool_call":
            raw_in_call: dict[str, Any] = (
                record["input"] if isinstance(record.get("input"), dict) else {}
            )
            ev_call = ToolEvent(
                event_id=event_id,
                event_index=event_index,
                tool_name=safe_str(record.get("tool_name") or record.get("name")),
                timestamp=timestamp,
                raw_input=raw_in_call,
                raw_output="",
                has_result=False,
            )
            transcript.events.append(ev_call)
            pending_calls[event_id] = ev_call
            continue

        if rec_type == "tool_result":
            ref_id = safe_str(record.get("tool_call_id") or record.get("id"))
            ev_call = pending_calls.get(ref_id)
            output = extract_text_content(record.get("output") or record.get("content"))
            is_error = bool(record.get("is_error", False))
            if ev_call is None:
                if len(transcript.parse_warnings) < MAX_PARSE_WARNINGS:
                    transcript.parse_warnings.append(
                        f"Line {line_no}: orphan tool_result for tool_call_id={ref_id!r}"
                    )
                continue
            ev_call.raw_output = output
            ev_call.is_error = is_error
            ev_call.has_result = True
            det_trunc, det_reason = detect_truncation(output, tool_name=ev_call.tool_name)
            ev_call.truncated = bool(record.get("truncated", False)) or det_trunc
            ev_call.truncation_reason = (
                safe_str(record.get("truncation_reason")) or det_reason or None
            )
            if not is_error:
                pag = detect_pagination(
                    event_id=ev_call.event_id,
                    event_index=ev_call.event_index,
                    tool_name=ev_call.tool_name,
                    timestamp=ev_call.timestamp,
                    raw_input=ev_call.raw_input,
                    raw_output=output,
                    is_error=False,
                )
                if pag is not None:
                    pag.truncated = ev_call.truncated
                    pag.truncation_reason = ev_call.truncation_reason
                    transcript.paginations.append(pag)
            continue

        if rec_type:
            if unrecognized_warnings < MAX_PARSE_WARNINGS:
                transcript.parse_warnings.append(
                    f"Line {line_no}: unrecognized generic record type {rec_type!r}"
                )
                unrecognized_warnings += 1
            else:
                suppressed_unrecognized += 1

    if suppressed_unrecognized:
        transcript.parse_warnings.append(
            f"... and {suppressed_unrecognized} additional unrecognized record warnings"
        )

    mark_followed_cursors(transcript.events, transcript.paginations)

    if transcript.assistant_messages:
        transcript.assistant_messages[-1].is_final = True
        transcript.final_text = transcript.assistant_messages[-1].text

    return transcript

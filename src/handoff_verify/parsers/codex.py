"""Parser for Codex CLI JSONL session transcripts (modern rollout & legacy flat formats)."""

from __future__ import annotations

import json
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
    load_jsonl,
    mark_followed_cursors,
    parse_numbered_lines,
    safe_int,
    safe_str,
    unwrap_shell_command,
)

_CODEX_EXIT_RE = re.compile(r"Process exited with code\s+(-?\d+)", re.IGNORECASE)
_CODEX_OUTPUT_MARKER_RE = re.compile(r"(?:^|\n)Output:\n", re.MULTILINE)
_PATCH_HEADER_RE = re.compile(
    r"^\*\*\*\s+(Update File|Add File|Delete File|Move to):\s*(.+?)\s*$",
    re.MULTILINE,
)

_SHELL_TOOLS = frozenset(
    {"shell", "local_shell", "exec_command", "shell_command", "container.exec"}
)
_READ_TOOLS = frozenset({"read_file", "view_file", "open_file"})
_SEARCH_TOOLS = frozenset({"grep_files", "search_files", "find_files", "rg"})
_LIST_TOOLS = frozenset({"list_dir", "list_files"})
_EDIT_TOOLS = frozenset({"edit_file", "write_file", "replace_file_content"})


@dataclass
class _PendingCodexCall:
    event_index: int
    event_id: str
    tool_name: str
    raw_input: dict[str, Any]
    timestamp: str | None
    raw_output: str = ""
    has_result: bool = False


def _parse_codex_arguments(raw_args: Any, line_no: int, warnings: list[str]) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        stripped = raw_args.strip()
        if not stripped:
            return {}
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
                return {"value": parsed}
            except json.JSONDecodeError as exc:
                warnings.append(f"Line {line_no}: malformed tool arguments JSON ({exc.msg})")
                return {"_raw": raw_args}
        return {"input": raw_args}
    if raw_args is None:
        return {}
    return {"value": raw_args}


def _unwrap_codex_output(raw_output: str) -> tuple[str, int | None, bool]:
    """Unwrap Codex JSON or header-prefixed outputs into (clean_output, exit_code, is_error)."""
    if not raw_output:
        return "", None, False

    stripped = raw_output.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and "output" in obj:
            inner_out = safe_str(obj.get("output"))
            meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
            exit_code = safe_int(meta.get("exit_code"))
            if exit_code is None:
                exit_code = safe_int(obj.get("exit_code"))
            is_err = bool(obj.get("is_error", False)) or (exit_code is not None and exit_code != 0)
            return inner_out, exit_code, is_err

    exit_code: int | None = None
    m_exit = _CODEX_EXIT_RE.search(raw_output)
    if m_exit:
        exit_code = int(m_exit.group(1))

    clean = raw_output
    m_marker = _CODEX_OUTPUT_MARKER_RE.search(raw_output)
    if m_marker:
        clean = raw_output[m_marker.end() :]

    is_err = (exit_code is not None and exit_code != 0) or clean.lstrip().startswith("Error:")
    return clean, exit_code, is_err


def _extract_patch_edits(
    patch_text: str,
    call: _PendingCodexCall,
    is_error: bool,
) -> list[FileEditEvent]:
    edits: list[FileEditEvent] = []
    for match in _PATCH_HEADER_RE.finditer(patch_text):
        action = match.group(1)
        file_path = match.group(2).strip()
        if action == "Add File":
            op = "write"
        elif action == "Delete File":
            op = "delete"
        else:
            op = "edit"
        edits.append(
            FileEditEvent(
                event_id=call.event_id,
                event_index=call.event_index,
                tool_name=call.tool_name,
                timestamp=call.timestamp,
                raw_input=call.raw_input,
                raw_output=call.raw_output,
                is_error=is_error,
                file_path=file_path,
                operation=op,
            )
        )
    return edits


def parse_codex(
    source: str | Path | Iterable[str],
    *,
    transcript_path: str | None = None,
) -> ParsedTranscript:
    """Parse a Codex CLI JSONL session log into a normalized ParsedTranscript."""
    loaded, detected_path = load_jsonl(source)
    transcript = ParsedTranscript(
        source_format="codex",
        transcript_path=transcript_path or detected_path,
        malformed_lines=loaded.malformed_lines,
        parse_warnings=list(loaded.parse_warnings),
    )

    pending_list: list[_PendingCodexCall] = []
    pending_by_id: dict[str, _PendingCodexCall] = {}
    fallback_agent_messages: list[tuple[str, str | None]] = []

    for line_no, record in loaded.records:
        rec_type = record.get("type")
        timestamp = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        payload = record.get("payload")

        if transcript.cwd is None and isinstance(record.get("cwd"), str) and record["cwd"].strip():
            transcript.cwd = record["cwd"].strip()

        # Modern Codex session metadata / turn context
        if rec_type in ("session_meta", "turn_context") and isinstance(payload, dict):
            if (
                transcript.cwd is None
                and isinstance(payload.get("cwd"), str)
                and payload["cwd"].strip()
            ):
                transcript.cwd = payload["cwd"].strip()
            continue

        # Modern Codex event_msg (e.g., agent_message)
        if rec_type == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "agent_message" and isinstance(payload.get("message"), str):
                text = payload["message"].strip()
                if text:
                    fallback_agent_messages.append((text, timestamp))
            continue

        # Normalize modern `response_item` wrapper vs legacy flat records
        item: dict[str, Any] = (
            payload if rec_type == "response_item" and isinstance(payload, dict) else record
        )

        item_type = item.get("type")

        if item_type == "function_call":
            call_id = safe_str(item.get("call_id") or item.get("id")) or f"call_{len(pending_list)}"
            tool_name = safe_str(item.get("name"))
            raw_in = _parse_codex_arguments(
                item.get("arguments"), line_no, transcript.parse_warnings
            )
            call = _PendingCodexCall(
                event_index=len(pending_list),
                event_id=call_id,
                tool_name=tool_name,
                raw_input=raw_in,
                timestamp=timestamp,
            )
            pending_list.append(call)
            pending_by_id[call_id] = call

        elif item_type == "custom_tool_call":
            call_id = safe_str(item.get("call_id") or item.get("id")) or f"call_{len(pending_list)}"
            tool_name = safe_str(item.get("name"))
            raw_in = _parse_codex_arguments(item.get("input"), line_no, transcript.parse_warnings)
            call = _PendingCodexCall(
                event_index=len(pending_list),
                event_id=call_id,
                tool_name=tool_name,
                raw_input=raw_in,
                timestamp=timestamp,
            )
            pending_list.append(call)
            pending_by_id[call_id] = call

        elif item_type in ("function_call_output", "custom_tool_call_output"):
            call_id = safe_str(item.get("call_id") or item.get("id"))
            call = pending_by_id.get(call_id)
            if call is None:
                transcript.parse_warnings.append(
                    f"Line {line_no}: orphan {item_type} for call_id={call_id!r}"
                )
                continue
            call.raw_output = extract_text_content(item.get("output"))
            call.has_result = True

        elif item_type == "message" and item.get("role") == "assistant":
            text = extract_text_content(item.get("content")).strip()
            if text:
                transcript.assistant_messages.append(
                    AssistantMessage(
                        index=len(transcript.assistant_messages),
                        text=text,
                        timestamp=timestamp,
                    )
                )

    # Fall back to event_msg agent_message only if no response_item assistant messages were found
    if not transcript.assistant_messages and fallback_agent_messages:
        for text, ts in fallback_agent_messages:
            transcript.assistant_messages.append(
                AssistantMessage(
                    index=len(transcript.assistant_messages),
                    text=text,
                    timestamp=ts,
                )
            )

    for call in pending_list:
        _materialize_codex_event(transcript, call)

    mark_followed_cursors(transcript.events, transcript.paginations)

    if transcript.assistant_messages:
        transcript.assistant_messages[-1].is_final = True
        transcript.final_text = transcript.assistant_messages[-1].text

    return transcript


def _materialize_codex_event(transcript: ParsedTranscript, call: _PendingCodexCall) -> None:
    name = call.tool_name
    raw_in = call.raw_input
    clean_out, exit_code, is_error = _unwrap_codex_output(call.raw_output)

    if name in _SHELL_TOOLS:
        cmd_raw = raw_in.get("command") or raw_in.get("cmd") or raw_in.get("input") or ""
        command = unwrap_shell_command(cmd_raw)
        cwd = safe_str(raw_in.get("workdir") or raw_in.get("cwd")) or transcript.cwd
        truncated, trunc_reason = detect_truncation(clean_out, tool_name=name)

        ev_cmd = CommandEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=call.raw_output,
            is_error=is_error,
            truncated=truncated,
            truncation_reason=trunc_reason,
            command=command,
            cwd=cwd,
            stdout=clean_out,
            stderr="",
            exit_code=exit_code,
        )
        transcript.events.append(ev_cmd)
        transcript.commands.append(ev_cmd)

        derived = analyze_shell_command(
            command=command,
            stdout=clean_out,
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=call.raw_output,
            is_error=is_error,
            exit_code=exit_code,
            truncated=truncated,
            truncation_reason=trunc_reason,
        )
        transcript.file_reads.extend(derived.file_reads)
        transcript.file_edits.extend(derived.file_edits)
        transcript.searches.extend(derived.searches)

        pag = detect_pagination(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=clean_out,
            is_error=is_error,
        )
        if pag is not None:
            transcript.paginations.append(pag)
        return

    if name == "apply_patch":
        patch_text = safe_str(raw_in.get("patch") or raw_in.get("input") or "")
        edits = _extract_patch_edits(patch_text, call, is_error)
        if edits:
            transcript.events.append(edits[0])
            if not is_error:
                transcript.file_edits.extend(edits)
        else:
            ev_fallback = FileEditEvent(
                event_id=call.event_id,
                event_index=call.event_index,
                tool_name=name,
                timestamp=call.timestamp,
                raw_input=raw_in,
                raw_output=call.raw_output,
                is_error=is_error,
                operation="edit",
            )
            transcript.events.append(ev_fallback)
            if not is_error:
                transcript.file_edits.append(ev_fallback)
        return

    if name in _READ_TOOLS:
        file_path = safe_str(raw_in.get("file_path") or raw_in.get("path"))
        offset = safe_int(raw_in.get("offset") or raw_in.get("start_line"))
        limit = safe_int(raw_in.get("limit"))
        if is_error:
            transcript.events.append(
                ToolEvent(
                    event_id=call.event_id,
                    event_index=call.event_index,
                    tool_name=name,
                    timestamp=call.timestamp,
                    raw_input=raw_in,
                    raw_output=call.raw_output,
                    is_error=True,
                )
            )
            return
        parsed_read = parse_numbered_lines(clean_out, offset=offset, limit=limit)
        ev_read = FileReadEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=call.raw_output,
            is_error=False,
            truncated=parsed_read.truncated,
            truncation_reason=parsed_read.truncation_reason,
            file_path=file_path,
            start_line=parsed_read.start_line,
            end_line=parsed_read.end_line,
            offset=offset,
            limit=limit,
            content=parsed_read.content,
            line_count=parsed_read.line_count,
        )
        transcript.events.append(ev_read)
        transcript.file_reads.append(ev_read)
        return

    if name in _SEARCH_TOOLS or name in _LIST_TOOLS:
        limit = safe_int(raw_in.get("limit") or raw_in.get("max_results"))
        lines = [line.strip() for line in clean_out.splitlines() if line.strip()]
        matched_files: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if line.startswith(("...", "[")):
                continue
            path_part = line.split(":", 1)[0].strip() if ":" in line else line
            if path_part and path_part not in seen:
                seen.add(path_part)
                matched_files.append(path_part)

        truncated, trunc_reason = detect_truncation(
            clean_out,
            tool_name=name,
            result_limit=limit,
            observed_count=len(lines),
        )
        ev_search = SearchEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=call.raw_output,
            is_error=is_error,
            truncated=truncated,
            truncation_reason=trunc_reason,
            query=safe_str(raw_in.get("pattern") or raw_in.get("query")),
            search_path=safe_str(raw_in.get("path") or raw_in.get("dir_path")) or None,
            glob_pattern=safe_str(raw_in.get("include") or raw_in.get("glob")) or None,
            match_count=len(lines),
            matched_files=matched_files,
            output=clean_out,
            result_limit=limit,
        )
        transcript.events.append(ev_search)
        transcript.searches.append(ev_search)
        return

    if name in _EDIT_TOOLS:
        op = "write" if name == "write_file" else "edit"
        ev_edit = FileEditEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=call.raw_output,
            is_error=is_error,
            file_path=safe_str(raw_in.get("file_path") or raw_in.get("path")),
            operation=op,
            old_text=raw_in.get("old_text") if isinstance(raw_in.get("old_text"), str) else None,
            new_text=raw_in.get("new_text") if isinstance(raw_in.get("new_text"), str) else None,
            content=raw_in.get("content") if isinstance(raw_in.get("content"), str) else None,
        )
        transcript.events.append(ev_edit)
        if not is_error:
            transcript.file_edits.append(ev_edit)
        return

    # MCP and any other custom tools
    truncated, trunc_reason = detect_truncation(clean_out, tool_name=name)
    ev_other = ToolEvent(
        event_id=call.event_id,
        event_index=call.event_index,
        tool_name=name,
        timestamp=call.timestamp,
        raw_input=raw_in,
        raw_output=call.raw_output,
        is_error=is_error,
        truncated=truncated,
        truncation_reason=trunc_reason,
    )
    transcript.events.append(ev_other)

    pag = detect_pagination(
        event_id=call.event_id,
        event_index=call.event_index,
        tool_name=name,
        timestamp=call.timestamp,
        raw_input=raw_in,
        raw_output=clean_out,
        is_error=is_error,
    )
    if pag is not None:
        pag.truncated = truncated
        pag.truncation_reason = trunc_reason
        transcript.paginations.append(pag)

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
    MAX_PARSE_WARNINGS,
    analyze_shell_command,
    detect_pagination,
    detect_truncation,
    extract_apply_patch_edits,
    extract_text_content,
    is_truncation_banner_line,
    load_jsonl,
    mark_followed_cursors,
    parse_numbered_lines,
    safe_int,
    safe_str,
    split_grep_path_prefix,
    split_lf_lines,
    unwrap_shell_command,
)

_CODEX_EXIT_RE = re.compile(
    r"(?:^|\n)\s*(?:Process exited with code|Exit code:?)\s+(-?\d+)\b",
    re.IGNORECASE,
)
_CODEX_OUTPUT_MARKER_RE = re.compile(r"(?:^|\n)Output:\r?\n", re.MULTILINE)
_CODEX_TOTAL_LINES_RE = re.compile(r"^Total output lines:\s*\d+\r?\n(?:\r?\n)?", re.IGNORECASE)
_CODEX_ERROR_PREFIXES = (
    "error:",
    "execution error:",
    "exec command rejected by user",
    "patch rejected by user",
    "command aborted by user",
    "aborted by user",
    "aborted before dispatch",
    "approval policy",
    "unsupported call:",
    "failed to parse function arguments:",
    "command timed out",
    "execution timed out",
    "rejected by user",
    "user rejected",
    "tool execution aborted",
)


def _is_codex_error_text(text: str) -> bool:
    low = text.lstrip().lower()
    return low.startswith(_CODEX_ERROR_PREFIXES)


_SHELL_TOOLS = frozenset(
    {
        "shell",
        "local_shell",
        "local_shell_call",
        "exec_command",
        "shell_command",
        "container.exec",
    }
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


def _parse_codex_arguments(
    raw_args: Any,
    line_no: int,
    warnings: list[str],
    suppressed_counter: list[int],
) -> dict[str, Any]:
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
            except (ValueError, RecursionError) as exc:
                if len(warnings) < MAX_PARSE_WARNINGS:
                    msg = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
                    warnings.append(f"Line {line_no}: malformed tool arguments JSON ({msg})")
                else:
                    suppressed_counter[0] += 1
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
        except (ValueError, RecursionError):
            obj = None
        if isinstance(obj, dict) and "output" in obj:
            inner_out = safe_str(obj.get("output"))
            meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
            exit_code = safe_int(meta.get("exit_code"))
            if exit_code is None:
                exit_code = safe_int(obj.get("exit_code"))
            if exit_code is not None:
                is_err = bool(obj.get("is_error", False)) or exit_code != 0
            else:
                is_err = bool(obj.get("is_error", False)) or _is_codex_error_text(inner_out)
            return inner_out, exit_code, is_err

    exit_code: int | None = None
    clean = raw_output
    m_marker = _CODEX_OUTPUT_MARKER_RE.search(raw_output[:1024])
    if m_marker:
        header = raw_output[: m_marker.start()]
        m_exit = _CODEX_EXIT_RE.search(header)
        if m_exit:
            exit_code = int(m_exit.group(1))
        clean = raw_output[m_marker.end() :]
        clean = _CODEX_TOTAL_LINES_RE.sub("", clean, count=1)
    else:
        first_line = raw_output.split("\n", 1)[0]
        m_exit = _CODEX_EXIT_RE.match(first_line)
        if m_exit:
            exit_code = int(m_exit.group(1))

    is_err = exit_code != 0 if exit_code is not None else _is_codex_error_text(clean)
    return clean, exit_code, is_err


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
    fallback_agent_messages: list[tuple[str, str | None, int | None]] = []
    suppressed_orphan_warnings = 0
    suppressed_malformed_args = [0]

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
                    after_idx = len(pending_list) - 1 if pending_list else None
                    fallback_agent_messages.append((text, timestamp, after_idx))
            continue

        # Normalize modern `response_item` wrapper vs legacy flat records
        item: dict[str, Any] = (
            payload if rec_type == "response_item" and isinstance(payload, dict) else record
        )

        item_type = item.get("type")

        if item_type == "function_call":
            call_id = safe_str(item.get("call_id") or item.get("id")) or f"call_{len(pending_list)}"
            if call_id in pending_by_id:
                continue
            tool_name = safe_str(item.get("name"))
            raw_in = _parse_codex_arguments(
                item.get("arguments"), line_no, transcript.parse_warnings, suppressed_malformed_args
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
            if call_id in pending_by_id:
                continue
            tool_name = safe_str(item.get("name"))
            raw_in = _parse_codex_arguments(
                item.get("input"), line_no, transcript.parse_warnings, suppressed_malformed_args
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

        elif item_type == "local_shell_call":
            call_id = safe_str(item.get("call_id") or item.get("id")) or f"call_{len(pending_list)}"
            if call_id in pending_by_id:
                continue
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            raw_in: dict[str, Any] = dict(action) if action else {}
            if "command" not in raw_in and "command" in item:
                raw_in["command"] = item["command"]
            call = _PendingCodexCall(
                event_index=len(pending_list),
                event_id=call_id,
                tool_name="local_shell_call",
                raw_input=raw_in,
                timestamp=timestamp,
            )
            pending_list.append(call)
            pending_by_id[call_id] = call

        elif item_type in (
            "function_call_output",
            "custom_tool_call_output",
            "local_shell_call_output",
        ):
            call_id = safe_str(item.get("call_id") or item.get("id"))
            call = pending_by_id.get(call_id)
            if call is None:
                if len(transcript.parse_warnings) < MAX_PARSE_WARNINGS:
                    transcript.parse_warnings.append(
                        f"Line {line_no}: orphan {item_type} for call_id={call_id!r}"
                    )
                else:
                    suppressed_orphan_warnings += 1
                continue
            call.raw_output = extract_text_content(item.get("output"))
            call.has_result = True

        elif item_type == "message" and item.get("role") == "assistant":
            text = extract_text_content(item.get("content")).strip()
            if text:
                after_idx = len(pending_list) - 1 if pending_list else None
                transcript.assistant_messages.append(
                    AssistantMessage(
                        index=len(transcript.assistant_messages),
                        text=text,
                        timestamp=timestamp,
                        after_event_index=after_idx,
                    )
                )

    if suppressed_malformed_args[0]:
        transcript.parse_warnings.append(
            f"... and {suppressed_malformed_args[0]} additional malformed tool argument warnings"
        )
    if suppressed_orphan_warnings:
        transcript.parse_warnings.append(
            f"... and {suppressed_orphan_warnings} additional orphan tool output warnings"
        )

    # Fall back to event_msg agent_message only if no response_item assistant messages were found
    if not transcript.assistant_messages and fallback_agent_messages:
        for text, ts, after_idx in fallback_agent_messages:
            transcript.assistant_messages.append(
                AssistantMessage(
                    index=len(transcript.assistant_messages),
                    text=text,
                    timestamp=ts,
                    after_event_index=after_idx,
                )
            )

    for call in pending_list:
        _materialize_codex_event(transcript, call)

    # Warn if tool calls existed but none mapped to typed read/edit/search/command/pagination events
    if (
        transcript.events
        and not transcript.file_reads
        and not transcript.file_edits
        and not transcript.searches
        and not transcript.commands
        and not transcript.paginations
    ):
        tool_names = sorted({ev.tool_name for ev in transcript.events if ev.tool_name})
        transcript.parse_warnings.append(
            f"Codex transcript had {len(transcript.events)} tool call(s) "
            f"({', '.join(tool_names[:5])}), none of which mapped to typed "
            f"read/edit/search/command events"
        )

    mark_followed_cursors(transcript.events, transcript.paginations)

    if transcript.assistant_messages:
        transcript.assistant_messages[-1].is_final = True
        transcript.final_text = transcript.assistant_messages[-1].text

    return transcript


def _materialize_codex_event(transcript: ParsedTranscript, call: _PendingCodexCall) -> None:
    name = call.tool_name
    raw_in = call.raw_input

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

    clean_out, exit_code, is_error = _unwrap_codex_output(call.raw_output)

    if name in _SHELL_TOOLS:
        cmd_raw = raw_in.get("command") or raw_in.get("cmd") or raw_in.get("input") or ""
        command = unwrap_shell_command(cmd_raw)
        cwd = (
            safe_str(raw_in.get("workdir") or raw_in.get("working_directory") or raw_in.get("cwd"))
            or transcript.cwd
        )
        truncated, trunc_reason = detect_truncation(clean_out, tool_name=name)

        # Only derive read/search/edit evidence when a real execution envelope produced
        # an explicit exit_code. Missing exit_code covers user rejections, aborts
        # (`Wall time: ...\naborted by user`, `aborted before dispatch`), policy denials,
        # parse errors, and still-running sessions (`Process running with session ID N`).
        cmd_is_error = is_error or (
            exit_code is None
            and not call.raw_output.lstrip().lower().startswith("process running with session id")
        )

        ev_cmd = CommandEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=call.raw_output,
            is_error=cmd_is_error,
            has_result=True,
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

        if exit_code is None:
            return

        derived = analyze_shell_command(
            command=command,
            stdout=clean_out,
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=call.raw_output,
            is_error=cmd_is_error,
            exit_code=exit_code,
            truncated=truncated,
            truncation_reason=trunc_reason,
        )
        transcript.file_reads.extend(derived.file_reads)
        transcript.file_edits.extend(derived.file_edits)
        transcript.searches.extend(derived.searches)

        if not derived.file_reads and not derived.searches:
            pag = detect_pagination(
                event_id=call.event_id,
                event_index=call.event_index,
                tool_name=name,
                timestamp=call.timestamp,
                raw_input=raw_in,
                raw_output=clean_out,
                is_error=cmd_is_error,
            )
            if pag is not None:
                transcript.paginations.append(pag)
        return

    if name == "apply_patch":
        # Require explicit exit_code == 0 or Codex's `Success...` confirmation before
        # recording an applied patch (rejections/aborts have no exit_code and no Success).
        patch_applied = (exit_code == 0) or (
            exit_code is None and not is_error and clean_out.lstrip().startswith("Success")
        )
        patch_is_error = not patch_applied
        patch_text = safe_str(raw_in.get("patch") or raw_in.get("input") or "")
        edits = extract_apply_patch_edits(
            patch_text,
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=call.raw_output,
            is_error=patch_is_error,
        )
        if edits:
            transcript.events.append(edits[0])
            if patch_applied:
                transcript.file_edits.extend(edits)
        else:
            ev_fallback = FileEditEvent(
                event_id=call.event_id,
                event_index=call.event_index,
                tool_name=name,
                timestamp=call.timestamp,
                raw_input=raw_in,
                raw_output=call.raw_output,
                is_error=patch_is_error,
                has_result=True,
                operation="edit",
            )
            transcript.events.append(ev_fallback)
            if patch_applied:
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
                    has_result=True,
                )
            )
            return
        parsed_read = parse_numbered_lines(
            clean_out, offset=offset, limit=limit, expect_line_numbers=True
        )
        ev_read = FileReadEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=call.raw_output,
            is_error=False,
            has_result=True,
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
                    has_result=True,
                )
            )
            return

        limit = safe_int(raw_in.get("limit") or raw_in.get("max_results"))
        lines = [
            line.strip()
            for line in split_lf_lines(clean_out)
            if line.strip()
            and line.strip().lower().rstrip(".")
            not in ("no matches found", "no files found", "no matching files found")
            and not is_truncation_banner_line(line)[0]
        ]
        matched_files: list[str] = []
        seen: set[str] = set()
        for line in lines:
            split = split_grep_path_prefix(line)
            path_part = split[0] if split is not None else line
            if path_part and path_part not in seen:
                seen.add(path_part)
                matched_files.append(path_part)

        truncated, trunc_reason = detect_truncation(
            clean_out,
            tool_name=name,
            result_limit=limit,
            observed_count=len(lines),
        )
        sp = safe_str(raw_in.get("path") or raw_in.get("dir_path")) or None
        ev_search = SearchEvent(
            event_id=call.event_id,
            event_index=call.event_index,
            tool_name=name,
            timestamp=call.timestamp,
            raw_input=raw_in,
            raw_output=call.raw_output,
            is_error=False,
            has_result=True,
            truncated=truncated,
            truncation_reason=trunc_reason,
            query=safe_str(raw_in.get("pattern") or raw_in.get("query")),
            search_path=sp,
            search_paths=[sp] if sp else [],
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
            has_result=True,
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

    # MCP and any other custom tools (including Codex Desktop `exec`)
    truncated, trunc_reason = detect_truncation(clean_out, tool_name=name)
    ev_other = ToolEvent(
        event_id=call.event_id,
        event_index=call.event_index,
        tool_name=name,
        timestamp=call.timestamp,
        raw_input=raw_in,
        raw_output=call.raw_output,
        is_error=is_error,
        has_result=True,
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

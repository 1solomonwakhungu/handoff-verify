"""Unit tests for the Codex CLI JSONL transcript parser."""

from __future__ import annotations

import json

from handoff_verify.parsers.codex import parse_codex


def _jsonl(records: list[dict | str]) -> list[str]:
    return [r if isinstance(r, str) else json.dumps(r) for r in records]


def test_codex_modern_rollout_format() -> None:
    lines = _jsonl(
        [
            {
                "timestamp": "2026-11-22T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": "codex-1", "cwd": "/work/project"},
            },
            {
                "timestamp": "2026-11-22T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_1",
                    "arguments": json.dumps({"cmd": "cat -n src/core.py"}),
                },
            },
            {
                "timestamp": "2026-11-22T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": (
                        "Chunk ID: abc\n"
                        "Wall time: 0.02 seconds\n"
                        "Process exited with code 0\n"
                        "Output:\n"
                        "     1\tdef run():\n"
                        "     2\t    return 42\n"
                    ),
                },
            },
            {
                "timestamp": "2026-11-22T12:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "call_2",
                    "input": (
                        "*** Begin Patch\n"
                        "*** Update File: src/core.py\n"
                        "@@\n-    return 42\n+    return 43\n"
                        "*** Add File: src/helper.py\n"
                        "+# helper\n"
                        "*** Delete File: src/old.py\n"
                        "*** End Patch"
                    ),
                },
            },
            {
                "timestamp": "2026-11-22T12:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call_2",
                    "output": json.dumps({"output": "Success", "metadata": {"exit_code": 0}}),
                },
            },
            {
                "timestamp": "2026-11-22T12:00:05Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Updated src/core.py:2."}],
                },
            },
            # Duplicate event_msg should NOT duplicate assistant_messages when response_item exists
            {
                "timestamp": "2026-11-22T12:00:05Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "Updated src/core.py:2."},
            },
        ]
    )

    parsed = parse_codex(lines)
    assert parsed.source_format == "codex"
    assert parsed.cwd == "/work/project"
    assert len(parsed.commands) == 1
    assert parsed.commands[0].exit_code == 0
    assert "def run():" in parsed.commands[0].stdout

    # cat -n derived FileReadEvent
    assert len(parsed.file_reads) == 1
    assert parsed.file_reads[0].file_path == "src/core.py"
    assert parsed.file_reads[0].start_line == 1
    assert parsed.file_reads[0].end_line == 2
    assert parsed.file_reads[0].content == "def run():\n    return 42"

    # apply_patch derived FileEditEvents
    assert [(e.file_path, e.operation) for e in parsed.file_edits] == [
        ("src/core.py", "edit"),
        ("src/helper.py", "write"),
        ("src/old.py", "delete"),
    ]

    # Single deduplicated final assistant message
    assert len(parsed.assistant_messages) == 1
    assert parsed.final_text == "Updated src/core.py:2."


def test_codex_legacy_flat_format_and_shell_argv_and_tools() -> None:
    lines = _jsonl(
        [
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "shell",
                "arguments": json.dumps({"command": ["bash", "-lc", "rg TODO src/"]}),
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps(
                    {
                        "output": "src/a.py:3:# TODO fix\n... (10 more matches)",
                        "metadata": {"exit_code": 0},
                    }
                ),
            },
            {
                "type": "function_call",
                "call_id": "c2",
                "name": "read_file",
                "arguments": json.dumps({"path": "README.md", "offset": 5, "limit": 10}),
            },
            {
                "type": "function_call_output",
                "call_id": "c2",
                "output": "5\tHello\n6\tWorld",
            },
            {
                "type": "function_call",
                "call_id": "c3",
                "name": "mcp__linear__list_issues",
                "arguments": json.dumps({"project": "core"}),
            },
            {
                "type": "function_call_output",
                "call_id": "c3",
                "output": "Showing page 1 of 4\nnext_cursor: tok_99",
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Reviewed all issues."}],
            },
        ]
    )

    parsed = parse_codex(lines)
    assert len(parsed.commands) == 1
    assert parsed.commands[0].command == "rg TODO src/"
    assert parsed.commands[0].truncated is True

    assert len(parsed.searches) == 1
    assert parsed.searches[0].query == "TODO"
    assert parsed.searches[0].search_path == "src/"
    assert parsed.searches[0].truncated is True

    assert len(parsed.file_reads) == 1
    assert parsed.file_reads[0].file_path == "README.md"
    assert parsed.file_reads[0].start_line == 5
    assert parsed.file_reads[0].end_line == 6

    assert len(parsed.paginations) == 1
    assert parsed.paginations[0].page == 1
    assert parsed.paginations[0].total_pages == 4
    assert parsed.paginations[0].has_more is True
    assert parsed.paginations[0].next_cursor == "tok_99"
    assert parsed.paginations[0].cursor_followed is False
    assert parsed.final_text == "Reviewed all issues."


def test_codex_malformed_arguments_and_event_msg_fallback() -> None:
    lines = _jsonl(
        [
            "{broken json line",
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "bad_args",
                    "name": "grep_files",
                    "arguments": "{invalid json",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "bad_args",
                    "output": "src/x.py:1:match",
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "Fallback reply."},
            },
        ]
    )

    parsed = parse_codex(lines)
    assert parsed.malformed_lines == 1
    assert any("malformed tool arguments" in w for w in parsed.parse_warnings)
    assert len(parsed.searches) == 1
    assert parsed.final_text == "Fallback reply."

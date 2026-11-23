"""Unit tests for the Claude Code JSONL transcript parser."""

from __future__ import annotations

import json
from pathlib import Path

from handoff_verify.parsers.claude_code import parse_claude_code


def _jsonl(records: list[dict | str]) -> list[str]:
    return [r if isinstance(r, str) else json.dumps(r) for r in records]


def test_claude_code_read_grep_edit_write_and_final_text(tmp_path: Path) -> None:
    records = [
        {
            "type": "assistant",
            "cwd": "/repo",
            "sessionId": "sess-1",
            "timestamp": "2026-11-22T10:00:00Z",
            "message": {
                "id": "msg_1",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Internal thought not in final text"},
                    {
                        "type": "tool_use",
                        "id": "toolu_read_1",
                        "name": "Read",
                        "input": {"file_path": "/repo/src/auth.py", "offset": 10, "limit": 5},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_grep_1",
                        "name": "Grep",
                        "input": {
                            "pattern": "verify_token",
                            "path": "src/",
                            "glob": "!vendor/**",
                            "output_mode": "content",
                            "head_limit": 2,
                        },
                    },
                ],
            },
        },
        # Return results out of order to verify chronological event_index preservation
        {
            "type": "user",
            "cwd": "/repo",
            "timestamp": "2026-11-22T10:00:01Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_grep_1",
                        "content": [
                            {"type": "text", "text": "src/auth.py:12:def verify_token():"},
                            {"type": "text", "text": "src/api.py:44:verify_token(req)"},
                        ],
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_read_1",
                        "content": (
                            "    10→# auth module\n"
                            "    11→\n"
                            "    12→def verify_token(tok):\n"
                            "    13→    return bool(tok)\n"
                            "<system-reminder>Do not leak secrets.</system-reminder>"
                        ),
                    },
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-11-22T10:00:02Z",
            "message": {
                "id": "msg_2",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_edit_1",
                        "name": "Edit",
                        "input": {
                            "file_path": "/repo/src/auth.py",
                            "old_string": "return bool(tok)",
                            "new_string": "return tok is not None",
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_write_1",
                        "name": "Write",
                        "input": {
                            "file_path": "/repo/src/new.py",
                            "content": "x = 1\n",
                        },
                    },
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-11-22T10:00:03Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_edit_1", "content": "OK"},
                    {"type": "tool_result", "tool_use_id": "toolu_write_1", "content": "OK"},
                ],
            },
        },
        # Split assistant message sharing msg_3 id
        {
            "type": "assistant",
            "timestamp": "2026-11-22T10:00:04Z",
            "message": {
                "id": "msg_3",
                "role": "assistant",
                "content": [{"type": "text", "text": "Updated /repo/src/auth.py:12-13."}],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-11-22T10:00:05Z",
            "message": {
                "id": "msg_3",
                "role": "assistant",
                "content": [{"type": "text", "text": "All callers checked."}],
            },
        },
    ]

    path = tmp_path / "claude.jsonl"
    path.write_text("\n".join(_jsonl(records)), encoding="utf-8")

    parsed = parse_claude_code(path)
    assert parsed.source_format == "claude_code"
    assert parsed.cwd == "/repo"
    assert parsed.malformed_lines == 0
    assert len(parsed.events) == 4
    assert [e.event_index for e in parsed.events] == [0, 1, 2, 3]

    # Verify Read line bounds and system-reminder stripping
    assert len(parsed.file_reads) == 1
    rd = parsed.file_reads[0]
    assert rd.file_path == "/repo/src/auth.py"
    assert rd.start_line == 10
    assert rd.end_line == 13
    assert rd.line_count == 4
    assert "system-reminder" not in rd.content
    assert "def verify_token(tok):" in rd.content

    # Verify Grep head_limit truncation and negative glob exclusion
    assert len(parsed.searches) == 1
    sr = parsed.searches[0]
    assert sr.query == "verify_token"
    assert sr.search_path == "src/"
    assert sr.excluded_dirs == ["vendor"]
    assert sr.matched_files == ["src/auth.py", "src/api.py"]
    assert sr.match_count == 2
    assert sr.truncated is True
    assert sr.truncation_reason is not None and "2" in sr.truncation_reason

    # Verify edits
    assert len(parsed.file_edits) == 2
    assert parsed.file_edits[0].operation == "edit"
    assert parsed.file_edits[0].event_index > rd.event_index
    assert parsed.file_edits[1].operation == "write"
    assert parsed.file_edits[1].file_path == "/repo/src/new.py"

    # Verify assistant message merging and thinking exclusion
    assert len(parsed.assistant_messages) == 1
    assert parsed.assistant_messages[0].is_final is True
    assert "Internal thought" not in parsed.final_text
    assert "Updated /repo/src/auth.py:12-13." in parsed.final_text
    assert "All callers checked." in parsed.final_text


def test_claude_code_errored_read_and_edit_excluded_from_success_lists() -> None:
    lines = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "missing.py"},
                        },
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "Edit",
                            "input": {
                                "file_path": "missing.py",
                                "old_string": "a",
                                "new_string": "b",
                            },
                        },
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "File does not exist.",
                            "is_error": True,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "t2",
                            "content": "Edit failed.",
                            "is_error": True,
                        },
                    ],
                },
            },
        ]
    )
    parsed = parse_claude_code(lines)
    assert len(parsed.events) == 2
    assert all(ev.is_error for ev in parsed.events)
    assert parsed.file_reads == []
    assert parsed.file_edits == []


def test_claude_code_mcp_pagination_and_cursor_tracking() -> None:
    lines = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "p1",
                            "name": "mcp__github__list_issues",
                            "input": {"repo": "acme/app"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "p1",
                            "content": json.dumps(
                                {
                                    "items": [{"id": 1}, {"id": 2}],
                                    "has_more": True,
                                    "nextCursor": "cur_page_2",
                                }
                            ),
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "p2",
                            "name": "mcp__github__list_issues",
                            "input": {"repo": "acme/app", "cursor": "cur_page_2"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "p2",
                            "content": json.dumps(
                                {
                                    "items": [{"id": 3}],
                                    "has_more": True,
                                    "nextCursor": "cur_page_3",
                                }
                            ),
                        }
                    ],
                },
            },
        ]
    )

    parsed = parse_claude_code(lines)
    assert len(parsed.paginations) == 2
    assert parsed.paginations[0].next_cursor == "cur_page_2"
    assert parsed.paginations[0].cursor_followed is True
    assert parsed.paginations[0].item_count == 2
    assert parsed.paginations[1].next_cursor == "cur_page_3"
    assert parsed.paginations[1].cursor_followed is False


def test_claude_code_bash_derived_events_and_multiedit_and_glob() -> None:
    lines = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "b1",
                            "name": "Bash",
                            "input": {"command": "rg --glob '!dist/**' foo src/ | head -n 2"},
                        },
                        {
                            "type": "tool_use",
                            "id": "b2",
                            "name": "Bash",
                            "input": {"command": "sed -n '20,22p' src/main.py"},
                        },
                        {
                            "type": "tool_use",
                            "id": "g1",
                            "name": "Glob",
                            "input": {"pattern": "**/*.py", "path": "src"},
                        },
                        {
                            "type": "tool_use",
                            "id": "m1",
                            "name": "MultiEdit",
                            "input": {
                                "file_path": "src/main.py",
                                "edits": [
                                    {"old_string": "a", "new_string": "b"},
                                    {"old_string": "c", "new_string": "d"},
                                ],
                            },
                        },
                        {
                            "type": "tool_use",
                            "id": "nb1",
                            "name": "NotebookEdit",
                            "input": {
                                "notebook_path": "analysis.ipynb",
                                "new_source": "x = 2",
                                "edit_mode": "replace",
                            },
                        },
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "b1",
                            "content": "src/a.py:1:foo\nsrc/b.py:2:foo",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "b2",
                            "content": "line 20\nline 21\nline 22",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "g1",
                            "content": "src/a.py\nsrc/b.py",
                        },
                        {"type": "tool_result", "tool_use_id": "m1", "content": "OK"},
                        {"type": "tool_result", "tool_use_id": "nb1", "content": "OK"},
                    ],
                },
            },
        ]
    )

    parsed = parse_claude_code(lines)
    assert len(parsed.commands) == 2
    # Bash rg | head -n 2 -> SearchEvent with truncation
    rg_searches = [s for s in parsed.searches if s.query == "foo"]
    assert len(rg_searches) == 1
    assert rg_searches[0].truncated is True
    assert rg_searches[0].excluded_dirs == ["dist"]
    assert rg_searches[0].search_path == "src/"
    # Bash sed -n -> FileReadEvent with start_line=20, end_line=22
    assert len(parsed.file_reads) == 1
    assert parsed.file_reads[0].file_path == "src/main.py"
    assert parsed.file_reads[0].start_line == 20
    assert parsed.file_reads[0].end_line == 22
    # Glob search
    glob_searches = [s for s in parsed.searches if s.tool_name == "Glob"]
    assert len(glob_searches) == 1
    assert glob_searches[0].matched_files == ["src/a.py", "src/b.py"]
    # MultiEdit (2 entries) + NotebookEdit (1 entry)
    assert len(parsed.file_edits) == 3


def test_claude_code_malformed_lines_and_grep_no_files_found() -> None:
    lines = [
        "{not valid json",
        "   ",
        "[1, 2, 3]",
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "g0",
                            "name": "Grep",
                            "input": {"pattern": "nonexistent", "path": "src"},
                        }
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "g0",
                            "content": "No files found",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "orphan_id",
                            "content": "stray",
                        },
                    ],
                },
            }
        ),
    ]
    parsed = parse_claude_code(lines)
    assert parsed.malformed_lines == 2
    assert len(parsed.parse_warnings) == 3  # 2 malformed + 1 orphan
    assert len(parsed.searches) == 1
    assert parsed.searches[0].matched_files == []
    assert parsed.searches[0].match_count == 0

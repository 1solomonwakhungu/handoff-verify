"""Adversarial edge-case tests covering real transcript pitfalls, shell parsing, and CLI safety."""

from __future__ import annotations

import json
import time
from pathlib import Path

from handoff_verify.cli import main
from handoff_verify.parsers import auto_parse, detect_format
from handoff_verify.parsers._common import detect_truncation, parse_numbered_lines
from handoff_verify.parsers.claude_code import parse_claude_code
from handoff_verify.parsers.codex import parse_codex
from handoff_verify.parsers.generic import parse_generic
from handoff_verify.pipeline import build_scope_receipt


def _jsonl(records: list[dict | str]) -> list[str]:
    return [r if isinstance(r, str) else json.dumps(r) for r in records]


# --- B1: Duplicate tool_use ids and non-assistant records ---


def test_b1_ignores_non_assistant_records_and_dedupes_tool_ids() -> None:
    tool_block = {
        "type": "tool_use",
        "id": "toolu_dup_1",
        "name": "Grep",
        "input": {"pattern": "secret", "path": "src"},
    }
    lines = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Checking."},
                        tool_block,
                    ],
                },
            },
            # Real Claude Code side-channel record repeating the assistant message
            {
                "type": "api-request-blob",
                "message": {
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Checking."},
                        tool_block,
                    ],
                },
            },
            # Duplicate tool_use id even inside an assistant record
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [tool_block],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_dup_1",
                            "content": "src/a.py",
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_2",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Done."}],
                },
            },
        ]
    )

    parsed = parse_claude_code(lines)
    assert len(parsed.events) == 1
    assert len(parsed.searches) == 1
    assert parsed.searches[0].match_count == 1
    assert [m.text for m in parsed.assistant_messages] == ["Checking.", "Done."]


# --- B2: Shell search counts (Exit code 1, || echo, | wc -l) ---


def test_b2_shell_search_exit_code_1_and_pipes_and_or_echo() -> None:
    lines = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "s1",
                            "name": "Bash",
                            "input": {"command": "grep -rn sym src/"},
                        },
                        {
                            "type": "tool_use",
                            "id": "s2",
                            "name": "Bash",
                            "input": {"command": "grep -rn sym src/ || echo 'No matches found'"},
                        },
                        {
                            "type": "tool_use",
                            "id": "s3",
                            "name": "Bash",
                            "input": {"command": "rg foo src | wc -l"},
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
                            "tool_use_id": "s1",
                            "content": "Exit code 1",
                            "is_error": True,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "s2",
                            "content": "No matches found",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "s3",
                            "content": "0",
                        },
                    ],
                },
            },
        ]
    )

    parsed = parse_claude_code(lines)
    assert len(parsed.searches) == 3

    # s1: Exit code 1 stripped -> 0 matches, empty files
    assert parsed.searches[0].match_count == 0
    assert parsed.searches[0].matched_files == []

    # s2: || echo writes to stdout -> match_count=None, matched_files=[]
    assert parsed.searches[1].match_count is None
    assert parsed.searches[1].matched_files == []

    # s3: | wc -l transforms stdout -> match_count=None, matched_files=[] (NOT ['0'])
    assert parsed.searches[2].match_count is None
    assert parsed.searches[2].matched_files == []


# --- B3: Unresolved and errored tool calls excluded from evidence and receipt ---


def test_b3_unresolved_and_errored_calls_excluded_from_typed_lists_and_receipt() -> None:
    lines = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "pending_read",
                            "name": "Read",
                            "input": {"file_path": "never_returned.py"},
                        },
                        {
                            "type": "tool_use",
                            "id": "pending_edit",
                            "name": "Edit",
                            "input": {
                                "file_path": "never_edited.py",
                                "old_string": "a",
                                "new_string": "b",
                            },
                        },
                        {
                            "type": "tool_use",
                            "id": "pending_grep",
                            "name": "Grep",
                            "input": {"pattern": "foo", "path": "pending_dir"},
                        },
                        {
                            "type": "tool_use",
                            "id": "err_grep",
                            "name": "Grep",
                            "input": {"pattern": "foo", "path": "/nope"},
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
                            "tool_use_id": "err_grep",
                            "content": "rg: /nope: No such file or directory",
                            "is_error": True,
                        }
                    ],
                },
            },
        ]
    )

    parsed = parse_claude_code(lines)
    assert len(parsed.events) == 4
    assert parsed.events[0].has_result is False
    assert parsed.events[1].has_result is False
    assert parsed.events[2].has_result is False
    assert parsed.events[3].has_result is True
    assert parsed.events[3].is_error is True

    assert parsed.file_reads == []
    assert parsed.file_edits == []
    assert parsed.searches == []

    receipt = build_scope_receipt(parsed)
    assert receipt.files_read == []
    assert receipt.files_edited == []
    assert receipt.searched_roots == []


# --- B4: Shell-derived read windows ---


def test_b4_shell_read_windows_piped_compound_sed_cd_and_tsv() -> None:
    lines = _jsonl(
        [
            {
                "type": "command",
                "command": "cat big.py | grep def",
                "stdout": "\n".join(f"def f{i}():" for i in range(30)),
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "head -50 f.py | tail -10",
                "stdout": "\n".join(f"line {i}" for i in range(41, 51)),
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "cat a.py && cat b.py",
                "stdout": "a1\na2\nb1\nb2",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "cat c.py; echo ---; cat d.py",
                "stdout": "c1\n---\nd1",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "sed -n '10,$p' tail_part.py",
                "stdout": "line10\nline11\nline12",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "cd src/handoff_verify && cat cli.py",
                "stdout": "import sys\nprint(1)",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "cat users.tsv",
                "stdout": "100\tAlice\n101\tBob\n102\tCarol",
                "exit_code": 0,
            },
        ]
    )

    parsed = parse_generic(lines)
    by_path = {r.file_path: r for r in parsed.file_reads}

    # Piped through grep -> unknown window (NOT 1-30)
    assert by_path["big.py"].start_line is None
    assert by_path["big.py"].end_line is None

    # head | tail -> unknown window (NOT 1-10)
    assert by_path["f.py"].start_line is None
    assert by_path["f.py"].end_line is None

    # Compound cat a.py && cat b.py -> both present with unknown windows
    assert by_path["a.py"].start_line is None
    assert by_path["b.py"].start_line is None

    # 3-part compound cat c.py; echo ---; cat d.py -> both recorded with unknown windows
    assert by_path["c.py"].start_line is None
    assert by_path["d.py"].start_line is None

    # sed -n '10,$p' -> start_line=10, end_line=12
    assert by_path["tail_part.py"].start_line == 10
    assert by_path["tail_part.py"].end_line == 12

    # cd src/handoff_verify && cat cli.py -> resolved path + exact window
    assert "src/handoff_verify/cli.py" in by_path
    assert by_path["src/handoff_verify/cli.py"].start_line == 1
    assert by_path["src/handoff_verify/cli.py"].end_line == 2

    # cat users.tsv -> preserves column 1, reports lines 1-3 (NOT 100-102)
    assert by_path["users.tsv"].start_line == 1
    assert by_path["users.tsv"].end_line == 3
    assert by_path["users.tsv"].content.startswith("100\tAlice")


# --- B5: Shell quoting, heredocs, newlines, cd resolution, multi-path, flags ---


def test_b5_shell_quoting_heredocs_newlines_cd_and_rg_flags() -> None:
    lines = _jsonl(
        [
            {
                "type": "command",
                "command": "rg 'foo => bar' src",
                "stdout": "src/a.py:1:foo => bar",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "grep 'self->name' src/a.py",
                "stdout": "src/a.py:2:self->name",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "cat data.txt | awk '$3 > 100'",
                "stdout": "row 200",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "cat <<'EOF'\n<div>hello</div>\nEOF",
                "stdout": "<div>hello</div>",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "cd packages/api\nrg foo",
                "stdout": "handler.py:5:foo",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "rg foo src\nrg bar tests",
                "stdout": "",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "cd packages/api && rg foo .",
                "stdout": "",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "rg --color never -M 200 needle src tests docs",
                "stdout": "",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "sed -i '' 's/a/b/' target.py",
                "stdout": "",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": (
                    "apply_patch <<'EOF'\n"
                    "*** Begin Patch\n"
                    "*** Update File: patched.py\n"
                    "*** End Patch\n"
                    "EOF"
                ),
                "stdout": "Success",
                "exit_code": 0,
            },
        ]
    )

    parsed = parse_generic(lines)

    # No bogus redirects from quoted `=>`, `->`, `awk '$3 > 100'`, or `<div>hello</div>`
    edited_paths = [e.file_path for e in parsed.file_edits]
    assert edited_paths == ["target.py", "patched.py"]

    # `cat data.txt | awk '$3 > 100'` was NOT suppressed by quoted `>`
    assert any(r.file_path == "data.txt" for r in parsed.file_reads)

    # Multi-line `cd packages/api\nrg foo` resolved search path to `packages/api`
    api_searches = [s for s in parsed.searches if s.search_path == "packages/api"]
    assert len(api_searches) == 2  # both newline `cd` and `&& rg foo .`

    # Multi-line `rg foo src\nrg bar tests` produced two searches
    assert any(s.query == "foo" and s.search_paths == ["src"] for s in parsed.searches)
    assert any(s.query == "bar" and s.search_paths == ["tests"] for s in parsed.searches)

    # `rg --color never -M 200 needle src tests docs` parsed query & all 3 paths cleanly
    multi = [s for s in parsed.searches if s.query == "needle"]
    assert len(multi) == 1
    assert multi[0].search_paths == ["src", "tests", "docs"]
    receipt = build_scope_receipt(parsed)
    for root in ("src", "tests", "docs", "packages/api"):
        assert root in receipt.searched_roots


# --- B6: Truncation & pagination precision and performance ---


def test_b6_truncation_content_false_positives_and_performance() -> None:
    # Grep output containing truncation words inside matched source lines must NOT flag
    grep_content = (
        'src/rate.py:10:raise RateLimitError("rate limit reached")\n'
        "src/doc.py:22:# more than the maximum allowed\n"
        'src/fmt.py:30:label = "[truncated]"\n'
        "src/note.py:40:# See (3 more files) in docs\n"
    )
    assert detect_truncation(grep_content) == (False, None)

    # Glob standalone banner IS detected and excluded from matched_files
    lines = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "g1",
                            "name": "Glob",
                            "input": {"pattern": "**/*.py"},
                        },
                        {
                            "type": "tool_use",
                            "id": "g2",
                            "name": "Grep",
                            "input": {"pattern": "build", "output_mode": "content"},
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
                            "tool_use_id": "g1",
                            "content": "src/a.py\nResults are truncated",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "g2",
                            "content": "[build-system]",
                        },
                    ],
                },
            },
        ]
    )
    parsed = parse_claude_code(lines)
    assert parsed.searches[0].matched_files == ["src/a.py"]
    assert parsed.searches[0].truncated is True
    # Content-mode line starting with `[` must NOT be dropped
    assert parsed.searches[1].match_count == 1

    # Performance regression guard: 10k blank lines and 5k unclosed `[` must be <100ms
    t0 = time.perf_counter()
    assert detect_truncation("\n" * 10_000) == (False, None)
    assert detect_truncation("[" * 5_000) == (False, None)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 100.0


def test_b6_pagination_graphql_final_short_cursor_offset_and_code_fp() -> None:
    lines = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        # 1. Final GraphQL page: hasNextPage=False with endCursor
                        {
                            "type": "tool_use",
                            "id": "gql_last",
                            "name": "mcp__gh__list_prs",
                            "input": {"repo": "acme/x"},
                        },
                        # 2. Short cursor "20" must NOT be marked followed by unrelated Read
                        {
                            "type": "tool_use",
                            "id": "short_cur",
                            "name": "mcp__api__list_items",
                            "input": {},
                        },
                        {
                            "type": "tool_use",
                            "id": "unrelated_read",
                            "name": "Read",
                            "input": {"file_path": "/logs/2026-11-20.log"},
                        },
                        # 3. Offset pagination: has_more=True followed by offset=20 on same tool
                        {
                            "type": "tool_use",
                            "id": "off_1",
                            "name": "mcp__api__list_users",
                            "input": {"offset": 0},
                        },
                        {
                            "type": "tool_use",
                            "id": "off_2",
                            "name": "mcp__api__list_users",
                            "input": {"offset": 20},
                        },
                        # 4. Bash reading source code with `next_cursor = response[...]` & prose
                        {
                            "type": "tool_use",
                            "id": "bash_code",
                            "name": "Bash",
                            "input": {
                                "command": "echo 'See Page 1 of 3\nnext_cursor = response[0]'"
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
                            "tool_use_id": "gql_last",
                            "content": json.dumps(
                                {
                                    "nodes": [{"id": 1}],
                                    "pageInfo": {"hasNextPage": False, "endCursor": "Y3Vyc29y"},
                                }
                            ),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "short_cur",
                            "content": json.dumps(
                                {"items": [], "has_more": True, "nextCursor": "20"}
                            ),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "unrelated_read",
                            "content": "     1→log line",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "off_1",
                            "content": json.dumps({"items": [{"id": 1}], "has_more": True}),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "off_2",
                            "content": json.dumps({"items": [], "has_more": False}),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "bash_code",
                            "content": "See Page 1 of 3\nnext_cursor = response[0]",
                        },
                    ],
                },
            },
        ]
    )

    parsed = parse_claude_code(lines)
    by_id = {p.event_id: p for p in parsed.paginations}

    # Final GraphQL page has has_more=False -> NOT in unread_cursors
    assert by_id["gql_last"].has_more is False
    # Short cursor "20" was NOT followed by Read("/logs/2026-11-20.log")
    assert by_id["short_cur"].cursor_followed is False
    # Offset pagination WAS followed by off_2
    assert by_id["off_1"].cursor_followed is True
    # Code/prose echo did NOT create a fake pagination event
    assert "bash_code" not in by_id

    receipt = build_scope_receipt(parsed)
    assert len(receipt.unread_cursors) == 1
    assert receipt.unread_cursors[0]["next_cursor"] == "20"


# --- B7: CLI safety on garbage inputs, pathological JSON, and stdin ---


def test_b7_garbage_and_pathological_json_lines(tmp_path: Path, capsys) -> None:
    # Empty file and markdown README must NOT return PASS (exit 2)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert main(["check", str(empty)]) == 2
    assert "no recognizable transcript events" in capsys.readouterr().err

    readme = tmp_path / "README.md"
    readme.write_text("# Hello\nNot a transcript.\n", encoding="utf-8")
    assert main(["check", str(readme)]) == 2
    assert "no recognizable transcript events" in capsys.readouterr().err

    # 5,000-digit integer + deeply nested JSON + valid record must count 2 malformed lines
    # without raising ValueError or RecursionError
    pathological = tmp_path / "pathological.jsonl"
    pathological.write_text(
        "\n".join(
            [
                '{"big": ' + ("9" * 5000) + "}",
                "[" * 2000 + "]" * 2000,
                json.dumps({"type": "assistant", "text": "Recovered cleanly."}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    parsed = auto_parse(pathological)
    assert parsed.malformed_lines == 2
    assert parsed.final_text == "Recovered cleanly."
    assert main(["check", str(pathological)]) == 0
    assert capsys.readouterr().out.startswith("PASS:")


# --- Non-blocking: form feed, in-file reminder, Codex realism, detection window ---


def test_form_feed_and_infile_system_reminder_preserved() -> None:
    raw = (
        "     1→# header\x0cpage2\n"
        "     2→tag = '<system-reminder>keep me</system-reminder>'\n"
        "<system-reminder>strip trailing reminder</system-reminder>"
    )
    parsed = parse_numbered_lines(raw, expect_line_numbers=True)
    assert parsed.start_line == 1
    assert parsed.end_line == 2
    assert parsed.line_count == 2
    assert "\x0c" in parsed.content
    assert "keep me" in parsed.content
    assert "strip trailing reminder" not in parsed.content


def test_codex_exit_code_colon_local_shell_call_and_exec_warning() -> None:
    # 1. Exit code: 1 header must mark failed read as error (NOT a FileReadEvent)
    #    and local_shell_call must parse as a CommandEvent
    lines = _jsonl(
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "c_fail",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "cat missing.py"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c_fail",
                    "output": (
                        "Exit code: 1\n"
                        "Wall time: 0.01s\n"
                        "Output:\n"
                        "cat: missing.py: No such file or directory\n"
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "local_shell_call",
                    "call_id": "lsc_1",
                    "action": {"command": ["bash", "-lc", "cat ok.py"]},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "local_shell_call_output",
                    "call_id": "lsc_1",
                    "output": json.dumps({"output": "x = 1\n", "metadata": {"exit_code": 0}}),
                },
            },
        ]
    )
    parsed = parse_codex(lines)
    assert parsed.commands[0].exit_code == 1
    assert parsed.commands[0].is_error is True
    assert [r.file_path for r in parsed.file_reads] == ["ok.py"]

    # 2. Codex Desktop custom `exec` JS sandbox calls emit a clear parse warning
    desktop_lines = _jsonl(
        [
            {"type": "session_meta", "payload": {"cwd": "/repo"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "js1",
                    "name": "exec",
                    "input": "console.log('hi')",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "js1",
                    "output": "hi",
                },
            },
        ]
    )
    parsed_desktop = parse_codex(desktop_lines)
    assert any("none of which mapped to typed" in w for w in parsed_desktop.parse_warnings)


def test_detect_format_past_60_summaries_and_generic_warning_cap() -> None:
    summaries = [
        json.dumps({"type": "summary", "summary": f"Summary {i}", "leafUuid": f"u{i}"})
        for i in range(65)
    ]
    summaries.append(
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi"}]},
            }
        )
    )
    assert detect_format(summaries) == "claude_code"

    unrecognized = [json.dumps({"type": f"weird_{i}"}) for i in range(80)]
    parsed_gen = parse_generic(unrecognized)
    assert len(parsed_gen.parse_warnings) == 51  # 50 + overflow summary
    assert "30 additional unrecognized" in parsed_gen.parse_warnings[-1]


def test_cli_stdin_dash_support(monkeypatch, capsys) -> None:
    import io

    payload = (
        json.dumps({"type": "file_read", "file_path": "x.py", "content": "x = 1"})
        + "\n"
        + json.dumps({"type": "assistant", "text": "Read x.py."})
        + "\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert main(["inspect", "-", "--format", "json"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["transcript_path"] == "<stdin>"
    assert inspected["files_read"] == ["x.py"]

    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert main(["check", "-"]) == 0
    assert capsys.readouterr().out.startswith("PASS:")


# --- Re-review blocking & non-blocking regression tests ---


def test_rereview_1_rejected_or_aborted_search_not_recorded_as_zero_matches() -> None:
    # Claude Code user-rejected Bash rg (is_error=True, no Exit code header)
    claude_lines = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "rej_1",
                            "name": "Bash",
                            "input": {"command": "rg -n TODO src/"},
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
                            "tool_use_id": "rej_1",
                            "content": "The user doesn't want to proceed with this tool use.",
                            "is_error": True,
                        }
                    ],
                },
            },
        ]
    )
    p_claude = parse_claude_code(claude_lines)
    assert p_claude.commands[0].exit_code is None
    assert p_claude.searches == []
    assert build_scope_receipt(p_claude).searched_roots == []

    # Generic errored command with no exit_code
    p_gen = parse_generic(
        _jsonl([{"type": "command", "command": "rg TODO src/", "stdout": "", "is_error": True}])
    )
    assert p_gen.searches == []

    # Codex plain-text "aborted by user"
    codex_lines = _jsonl(
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "ab1",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "rg TODO src/"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "ab1",
                    "output": "aborted by user",
                },
            },
        ]
    )
    p_codex = parse_codex(codex_lines)
    assert p_codex.commands[0].is_error is True
    assert p_codex.searches == []


def test_rereview_2_single_file_grep_and_count_quiet_flags() -> None:
    lines = _jsonl(
        [
            {
                "type": "command",
                "command": "grep import src/a.py",
                "stdout": "import os\nimport sys\n",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "grep -c TODO src/a.py",
                "stdout": "5\n",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "grep -q TODO src/a.py",
                "stdout": "",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "rg foo src/",
                "stdout": "rg: src/secret: Permission denied\nsrc/ok.py:1:foo\n",
                "exit_code": 0,
            },
        ]
    )
    parsed = parse_generic(lines)
    assert len(parsed.searches) == 4

    # Single-file grep counts non-colon output lines accurately
    assert parsed.searches[0].match_count == 2
    assert parsed.searches[0].matched_files == ["src/a.py"]

    # -c and -q return match_count=None (not 0)
    assert parsed.searches[1].match_count is None
    assert parsed.searches[2].match_count is None

    # `rg: ... Permission denied` is NOT counted as a match in file `rg`
    assert parsed.searches[3].match_count == 1
    assert parsed.searches[3].matched_files == ["src/ok.py"]


def test_rereview_3_mid_output_elision_and_parenthesized_truncation() -> None:
    # Mid-output Claude Code and Codex elision markers
    mid_claude = "\n".join(
        [f"line {i}" for i in range(1, 51)]
        + ["... [4000 lines truncated] ..."]
        + [f"line {i}" for i in range(4051, 4101)]
    )
    assert detect_truncation(mid_claude)[0] is True

    mid_codex = "\n".join(
        [f"line {i}" for i in range(1, 51)]
        + ["[... omitted 4000 of 4400 lines ...]"]
        + [f"line {i}" for i in range(4051, 4101)]
    )
    assert detect_truncation(mid_codex)[0] is True

    # Parenthesized truncation banner
    assert detect_truncation("foo\n(Results are truncated. Showing partial output.)")[0] is True

    # `cat big.txt` with mid-output elision must NOT claim a contiguous (1, N) window
    parsed = parse_generic(
        _jsonl([{"type": "command", "command": "cat big.txt", "stdout": mid_codex, "exit_code": 0}])
    )
    assert len(parsed.file_reads) == 1
    rd = parsed.file_reads[0]
    assert rd.truncated is True
    assert rd.start_line is None
    assert rd.end_line is None


def test_rereview_4_forced_parser_and_summary_only_never_pass(tmp_path: Path, capsys) -> None:
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("not json at all\n", encoding="utf-8")
    for parser_name in ("claude_code", "codex", "generic"):
        assert main(["check", str(garbage), "--parser", parser_name]) == 2
        assert "no recognizable transcript events" in capsys.readouterr().err

    summary_only = tmp_path / "summary_only.jsonl"
    summary_only.write_text(
        json.dumps({"type": "summary", "summary": "Title", "leafUuid": "u1"}) + "\n",
        encoding="utf-8",
    )
    assert main(["check", str(summary_only)]) == 2
    assert "no recognizable transcript events" in capsys.readouterr().err


def test_rereview_5_find_and_fd_flag_values_not_roots() -> None:
    lines = _jsonl(
        [
            {
                "type": "command",
                "command": "find . -type f -name '*.py'",
                "stdout": "./a.py\n",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "find src -maxdepth 2 -type d -not -path '*/node_modules/*'",
                "stdout": "src/pkg\n",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "find src -name '*.py' -exec grep foo {} +",
                "stdout": "src/a.py\n",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "fd -e py foo src",
                "stdout": "src/foo.py\n",
                "exit_code": 0,
            },
        ]
    )
    parsed = parse_generic(lines)
    assert parsed.searches[0].search_paths == ["."]
    assert parsed.searches[0].glob_pattern == "*.py"

    assert parsed.searches[1].search_paths == ["src"]
    assert parsed.searches[1].excluded_dirs == ["node_modules"]

    assert parsed.searches[2].search_paths == ["src"]

    assert parsed.searches[3].search_paths == ["src"]
    assert parsed.searches[3].glob_pattern == "foo"


def test_rereview_nonblocking_bom_heredoc_cat_nested_gql_cursor_err_and_perf(
    tmp_path: Path,
) -> None:
    # 1. UTF-8 BOM file parses cleanly and passes check
    bom_file = tmp_path / "bom.jsonl"
    bom_file.write_bytes(
        b"\xef\xbb\xbf"
        + json.dumps({"type": "assistant", "text": "Clean."}).encode("utf-8")
        + b"\n"
    )
    assert detect_format(bom_file) == "generic"
    assert main(["check", str(bom_file)]) == 0

    # 2. `cat <<EOF ... EOF` must NOT create FileReadEvent("<<EOF"),
    #    line continuation `rg TODO \\\nsrc tests` has no '\n',
    #    `cd pkg && apply_patch <<'EOF'` resolves `pkg/a.py`,
    #    and Codex argv `["apply_patch", "<patch>"]` extracts edit
    shell_lines = _jsonl(
        [
            {
                "type": "command",
                "command": "cat <<EOF\nhello\nEOF",
                "stdout": "hello",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "rg TODO \\\nsrc tests",
                "stdout": "",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": (
                    "cd pkg && apply_patch <<'EOF'\n"
                    "*** Begin Patch\n"
                    "*** Update File: a.py\n"
                    "*** End Patch\n"
                    "EOF"
                ),
                "stdout": "Success",
                "exit_code": 0,
            },
            {
                "type": "command",
                "command": "apply_patch '*** Begin Patch\n*** Add File: b.py\n*** End Patch'",
                "stdout": "Success",
                "exit_code": 0,
            },
        ]
    )
    p_shell = parse_generic(shell_lines)
    assert p_shell.file_reads == []
    assert p_shell.searches[0].search_paths == ["src", "tests"]
    assert [(e.file_path, e.operation) for e in p_shell.file_edits] == [
        ("pkg/a.py", "edit"),
        ("b.py", "write"),
    ]

    # 3. Nested GraphQL data.repository.issues.pageInfo + errored follow-up + different resource
    gql_lines = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "gql1",
                            "name": "mcp__gh__query",
                            "input": {"repo": "org/a"},
                        },
                        {
                            "type": "tool_use",
                            "id": "gql2_err",
                            "name": "mcp__gh__query",
                            "input": {"repo": "org/a", "after": "CUR_1"},
                        },
                        {
                            "type": "tool_use",
                            "id": "pg1",
                            "name": "mcp__gh__list",
                            "input": {"repo": "org/a", "page": 1},
                        },
                        {
                            "type": "tool_use",
                            "id": "pg2_other_repo",
                            "name": "mcp__gh__list",
                            "input": {"repo": "org/b", "page": 2},
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
                            "tool_use_id": "gql1",
                            "content": json.dumps(
                                {
                                    "data": {
                                        "repository": {
                                            "issues": {
                                                "nodes": [{"id": 1}],
                                                "pageInfo": {
                                                    "hasNextPage": True,
                                                    "endCursor": "CUR_1",
                                                },
                                            }
                                        }
                                    }
                                }
                            ),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "gql2_err",
                            "content": "500 Internal Server Error",
                            "is_error": True,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "pg1",
                            "content": json.dumps({"items": [], "page": 1, "total_pages": 3}),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "pg2_other_repo",
                            "content": json.dumps({"items": [], "page": 2, "total_pages": 2}),
                        },
                    ],
                },
            },
        ]
    )
    p_gql = parse_claude_code(gql_lines)
    by_id = {p.event_id: p for p in p_gql.paginations}
    assert "gql1" in by_id
    assert by_id["gql1"].next_cursor == "CUR_1"
    assert by_id["gql1"].cursor_followed is False  # errored follow-up does NOT count
    assert by_id["pg1"].cursor_followed is False  # page 2 of org/b does NOT follow org/a

    # 4. Linear shell tokenizer performance on 200KB quoted argument (<100ms)
    big_cmd = "echo '" + ("a" * 200_000) + "'"
    t0 = time.perf_counter()
    parse_generic(_jsonl([{"type": "command", "command": big_cmd, "stdout": "ok", "exit_code": 0}]))
    assert (time.perf_counter() - t0) * 1000.0 < 200.0

    # 5. Replayed multi-block assistant text with same msg_id does not double,
    #    Grep `[Showing results with pagination = ...]` not counted as match,
    #    and `Exit code 3` inside file content is not read as command exit code.
    claude_misc = _jsonl(
        [
            {
                "type": "assistant",
                "message": {
                    "id": "m_rep",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "T1"}, {"type": "text", "text": "T2"}],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "m_rep",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "T1"}, {"type": "text", "text": "T2"}],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "gp1",
                            "name": "Grep",
                            "input": {"pattern": "foo"},
                        },
                        {
                            "type": "tool_use",
                            "id": "b_ec",
                            "name": "Bash",
                            "input": {"command": "cat doc.txt"},
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
                            "tool_use_id": "gp1",
                            "content": "src/a.py\n[Showing results with pagination = 10]",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "b_ec",
                            "content": "Line 1\nExit code 3 means invalid config\nLine 3",
                        },
                    ],
                },
            },
        ]
    )
    p_misc = parse_claude_code(claude_misc)
    assert p_misc.assistant_messages[0].text == "T1\nT2"
    assert p_misc.searches[0].matched_files == ["src/a.py"]
    assert p_misc.searches[0].match_count == 1
    assert p_misc.commands[0].exit_code == 0

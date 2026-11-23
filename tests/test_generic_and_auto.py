"""Unit tests for the generic parser, format auto-detection, and truncation heuristics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff_verify.parsers import auto_parse, detect_format
from handoff_verify.parsers._common import detect_truncation, parse_numbered_lines
from handoff_verify.parsers.generic import parse_generic


def test_generic_parser_all_record_types() -> None:
    lines = [
        json.dumps({"type": "meta", "cwd": "/repo"}),
        json.dumps(
            {
                "type": "file_read",
                "file_path": "src/app.py",
                "offset": 1,
                "content": "def main():\n    return 0",
            }
        ),
        json.dumps(
            {
                "type": "file_edit",
                "file_path": "src/app.py",
                "operation": "edit",
                "old_text": "return 0",
                "new_text": "return 1",
            }
        ),
        json.dumps(
            {
                "type": "search",
                "tool_name": "grep",
                "query": "main",
                "search_path": "src",
                "excluded_dirs": ["build"],
                "matched_files": ["src/app.py"],
                "result_limit": 1,
            }
        ),
        json.dumps(
            {
                "type": "command",
                "command": "pytest -q",
                "stdout": "1 passed",
                "exit_code": 0,
            }
        ),
        json.dumps(
            {
                "type": "pagination",
                "tool_name": "list_prs",
                "resource": "pulls",
                "page": 1,
                "total_pages": 2,
                "next_cursor": "nxt_2",
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "id": "tc1",
                "tool_name": "list_prs",
                "input": {"cursor": "nxt_2"},
            }
        ),
        json.dumps(
            {
                "type": "tool_result",
                "tool_call_id": "tc1",
                "output": json.dumps({"items": [], "has_more": False}),
            }
        ),
        json.dumps({"type": "assistant", "text": "All checks done."}),
    ]

    parsed = parse_generic(lines)
    assert parsed.source_format == "generic"
    assert parsed.cwd == "/repo"
    assert len(parsed.file_reads) == 1
    assert parsed.file_reads[0].start_line == 1
    assert parsed.file_reads[0].end_line == 2
    assert len(parsed.file_edits) == 1
    assert len(parsed.searches) == 1
    assert parsed.searches[0].truncated is True  # result_limit=1 reached
    assert len(parsed.commands) == 1
    assert len(parsed.paginations) == 2
    assert parsed.paginations[0].cursor_followed is True
    assert parsed.paginations[1].has_more is False
    assert parsed.final_text == "All checks done."


def test_detect_format_and_auto_parse(tmp_path: Path) -> None:
    claude_file = tmp_path / "claude.jsonl"
    claude_file.write_text(
        "\n".join(
            [
                "{broken first line",
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "s1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Hi"}],
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    assert detect_format(claude_file) == "claude_code"
    p_claude = auto_parse(claude_file)
    assert p_claude.source_format == "claude_code"
    assert p_claude.malformed_lines == 1
    assert p_claude.final_text == "Hi"

    codex_file = tmp_path / "codex.jsonl"
    codex_file.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": "/repo"}}) + "\n",
        encoding="utf-8",
    )
    assert detect_format(codex_file) == "codex"
    assert auto_parse(codex_file).source_format == "codex"

    generic_file = tmp_path / "generic.jsonl"
    generic_file.write_text(
        json.dumps({"type": "file_read", "file_path": "a.py", "content": "x = 1"}) + "\n",
        encoding="utf-8",
    )
    assert detect_format(generic_file) == "generic"
    assert auto_parse(generic_file).source_format == "generic"

    # Generator input works cleanly (materialized once inside auto_parse)
    gen = (line for line in [json.dumps({"type": "assistant", "text": "From gen"})])
    p_gen = auto_parse(gen)
    assert p_gen.source_format == "generic"
    assert p_gen.final_text == "From gen"

    # Empty / unknown file
    empty_file = tmp_path / "empty.jsonl"
    empty_file.write_text("\n\n", encoding="utf-8")
    assert detect_format(empty_file) == "unknown"
    assert auto_parse(empty_file).source_format == "unknown"

    # Invalid format_hint raises ValueError
    with pytest.raises(ValueError, match="Unsupported transcript format"):
        auto_parse(generic_file, format_hint="bogus")


def test_truncation_detection_avoids_false_positives() -> None:
    # Complete count must NOT flag as truncated
    assert detect_truncation("Showing first 5 of 5 results") == (False, None)
    assert detect_truncation("Showing lines 1-10 of 10") == (False, None)
    # Normal code mentioning the word truncated in an identifier must NOT flag
    assert detect_truncation("is_truncated = False\nreturn is_truncated") == (False, None)

    # Genuine truncation markers MUST flag
    assert detect_truncation("Showing first 5 of 20 results")[0] is True
    assert detect_truncation("... (42 more lines)")[0] is True
    assert detect_truncation("Output truncated after 200 lines")[0] is True
    assert detect_truncation(json.dumps({"truncated": True, "items": []}))[0] is True


def test_parse_numbered_lines_guards_against_non_monotonic_tsv() -> None:
    tsv = "1\talpha\n1\tbeta\n2\tgamma"
    parsed = parse_numbered_lines(tsv)
    # Non-monotonic leading digits should keep content intact rather than stripping column 1
    assert parsed.content == tsv
    assert parsed.start_line == 1
    assert parsed.end_line == 3

    cat_n = "   10\tdef a():\n   11\t    pass\n... (5 more lines)"
    parsed2 = parse_numbered_lines(cat_n)
    assert parsed2.start_line == 10
    assert parsed2.end_line == 11
    assert parsed2.content == "def a():\n    pass"
    assert parsed2.truncated is True

"""End-to-end verification pipeline tests across Claude Code, Codex, and Generic transcripts."""

from __future__ import annotations

import json
from pathlib import Path

from handoff_verify.models import AssistantMessage, FileReadEvent, ParsedTranscript
from handoff_verify.parsers import auto_parse
from handoff_verify.pipeline import verify_transcript


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_e2e_clean_claude_code_session(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "def authenticate(user, secret):\n    return verify_token(user, secret)\n",
        encoding="utf-8",
    )
    transcript_file = _write_jsonl(
        tmp_path / "claude_clean.jsonl",
        [
            {
                "type": "assistant",
                "cwd": str(tmp_path),
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": str(tmp_path / "src" / "auth.py")},
                        },
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "Grep",
                            "input": {"pattern": "authenticate"},
                        },
                    ],
                },
            },
            {
                "type": "user",
                "cwd": str(tmp_path),
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": (
                                "     1→def authenticate(user, secret):\n"
                                "     2→    return verify_token(user, secret)"
                            ),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "t2",
                            "content": "src/auth.py",
                        },
                    ],
                },
            },
            {
                "type": "assistant",
                "cwd": str(tmp_path),
                "message": {
                    "id": "m2",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "In `src/auth.py:2`, it calls `verify_token(user, secret)`. "
                                "There are no other callers in the repository."
                            ),
                        }
                    ],
                },
            },
        ],
    )
    parsed = auto_parse(transcript_file)
    report = verify_transcript(parsed, repo_root=tmp_path)
    assert report.passed is True
    assert report.findings == []
    assert len(report.claims) == 3  # pointer + quote + absence
    assert report.duration_ms < 50.0


def test_e2e_planted_failures_claude_code_session(tmp_path: Path) -> None:
    transcript_file = _write_jsonl(
        tmp_path / "claude_bad.jsonl",
        [
            {
                "type": "assistant",
                "cwd": "/repo",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "/repo/src/auth.py", "offset": 1, "limit": 5},
                        },
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "Grep",
                            "input": {
                                "pattern": "legacy_auth",
                                "path": "src/auth",
                                "head_limit": 2,
                            },
                        },
                    ],
                },
            },
            {
                "type": "user",
                "cwd": "/repo",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "     1→def login(u):\n     2→    return check(u)",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "t2",
                            "content": "src/auth/a.py\nsrc/auth/b.py",
                        },
                    ],
                },
            },
            {
                "type": "assistant",
                "cwd": "/repo",
                "message": {
                    "id": "m2",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Checked `src/auth.py:42` which calls `bypass_all(admin=True)`. "
                                "Also inspected `src/billing.py:10` and found no other callers."
                            ),
                        }
                    ],
                },
            },
        ],
    )
    parsed = auto_parse(transcript_file)
    report = verify_transcript(parsed)
    assert report.passed is False
    codes = {f.code for f in report.findings}
    assert "LINE_OUT_OF_READ_WINDOW" in codes
    assert "QUOTE_NOT_FOUND" in codes
    assert "UNREAD_FILE" in codes
    assert "TRUNCATED_SEARCH_OUTPUT" in codes
    assert "NARROW_SEARCH_SCOPE" in codes


def test_e2e_codex_session_clean_and_failing(tmp_path: Path) -> None:
    codex_file = _write_jsonl(
        tmp_path / "codex_fail.jsonl",
        [
            {"type": "session_meta", "payload": {"cwd": "/repo"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "list_issues",
                    "arguments": json.dumps({"repo": "acme/app"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": json.dumps(
                        {
                            "output": json.dumps(
                                {"items": [{"id": 1}], "has_more": True, "next_cursor": "nxt_2"}
                            ),
                            "metadata": {"exit_code": 0},
                        }
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Reviewed all pages of issues; see `src/unopened.py:99`.",
                        }
                    ],
                },
            },
        ],
    )
    parsed = auto_parse(codex_file)
    report = verify_transcript(parsed)
    assert report.passed is False
    codes = {f.code for f in report.findings}
    assert "UNREAD_PAGINATION_CURSOR" in codes
    assert "UNREAD_FILE" in codes


def test_strict_mode_fails_on_warning(tmp_path: Path) -> None:
    transcript_file = _write_jsonl(
        tmp_path / "warn_only.jsonl",
        [
            {"type": "meta", "cwd": "/repo"},
            {
                "type": "search",
                "tool_name": "grep",
                "query": "foo",
                "search_path": ".",
                "excluded_dirs": ["vendor"],
                "match_count": 0,
            },
            {"type": "assistant", "text": "Found no matches anywhere in the repo."},
        ],
    )
    parsed = auto_parse(transcript_file)
    report_normal = verify_transcript(parsed, strict=False)
    assert report_normal.passed is True
    assert [f.code for f in report_normal.findings] == ["EXCLUDED_SEARCH_DIRS"]
    assert report_normal.findings[0].severity == "warning"

    report_strict = verify_transcript(parsed, strict=True)
    assert report_strict.passed is False


def test_latency_budget_on_large_transcript() -> None:
    reads = [
        FileReadEvent(
            event_index=i,
            tool_name="Read",
            file_path=f"src/mod_{i}.py",
            start_line=1,
            end_line=50,
            line_count=50,
            content="\n".join(f"def fn_{i}_{j}(x): return x + {j}" for j in range(1, 51)),
        )
        for i in range(100)
    ]
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=reads,
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Checked `src/mod_42.py:10` which defines `def fn_42_10(x): return x + 10`.",
                is_final=True,
            )
        ],
    )
    report = verify_transcript(tr)
    assert report.passed is True
    assert report.duration_ms < 50.0

"""Unit tests for core data models and JSON serialization."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from handoff_verify import __version__
from handoff_verify.models import (
    AssistantMessage,
    Claim,
    CommandEvent,
    FileEditEvent,
    FileReadEvent,
    Finding,
    PaginationEvent,
    ParsedTranscript,
    ScopeReceipt,
    SearchEvent,
    ToolEvent,
    VerificationReport,
)


def test_version_exported() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert __version__ == expected == "0.1.1"


def test_typed_events_inherit_tool_event_and_serialize() -> None:
    read_ev = FileReadEvent(
        event_id="e1",
        event_index=0,
        tool_name="Read",
        file_path="src/app.py",
        start_line=1,
        end_line=20,
        offset=1,
        limit=20,
        content="print('hi')",
        line_count=1,
    )
    edit_ev = FileEditEvent(
        event_id="e2",
        event_index=1,
        tool_name="Edit",
        file_path="src/app.py",
        operation="edit",
        old_text="hi",
        new_text="hello",
    )
    search_ev = SearchEvent(
        event_id="e3",
        event_index=2,
        tool_name="Grep",
        query="hello",
        search_path="src",
        excluded_dirs=["vendor"],
        match_count=1,
        matched_files=["src/app.py"],
        truncated=True,
        truncation_reason="head_limit (1) reached",
        result_limit=1,
    )
    cmd_ev = CommandEvent(
        event_id="e4",
        event_index=3,
        tool_name="Bash",
        command="pytest",
        cwd="/repo",
        stdout="1 passed",
        exit_code=0,
    )
    pag_ev = PaginationEvent(
        event_id="e5",
        event_index=4,
        tool_name="mcp__github__list_issues",
        resource="issues",
        page=1,
        total_pages=3,
        has_more=True,
        next_cursor="cursor_2",
        cursor_followed=False,
        item_count=20,
    )

    for ev in (read_ev, edit_ev, search_ev, cmd_ev, pag_ev):
        assert isinstance(ev, ToolEvent)
        d = ev.to_dict()
        # Must round-trip cleanly through json.dumps
        assert json.loads(json.dumps(d)) == d

    assert read_ev.to_dict()["file_path"] == "src/app.py"
    assert pag_ev.to_dict()["next_cursor"] == "cursor_2"


def test_verification_report_to_dict_json_safe() -> None:
    claim = Claim(
        kind="pointer",
        text="src/app.py:42",
        file_path="src/app.py",
        start_line=42,
        end_line=42,
        message_index=0,
    )
    finding = Finding(
        severity="error",
        code="UNREAD_LINE_RANGE",
        claim=claim,
        evidence={"read_end": 20, "repo": Path("/tmp/repo")},
        message="Line 42 was outside read window 1-20",
        suggestion="Read lines 21-60 of src/app.py",
    )
    receipt = ScopeReceipt(
        searched_roots=["src"],
        excluded_dirs=["node_modules"],
        truncated_outputs=[{"event_index": 2, "tool_name": "Grep", "reason": "limit reached"}],
        unread_cursors=[{"event_index": 4, "tool_name": "list_issues", "next_cursor": "c2"}],
        files_read=["src/app.py"],
        files_edited=["src/app.py"],
    )
    report = VerificationReport(
        transcript_path="/tmp/session.jsonl",
        source_format="claude_code",
        claims=[claim],
        findings=[finding],
        scope_receipt=receipt,
        passed=False,
        duration_ms=1.25,
    )

    data = report.to_dict()
    encoded = json.dumps(data)
    decoded = json.loads(encoded)
    assert decoded["schema_version"] == "1.0"
    assert decoded["passed"] is False
    assert decoded["findings"][0]["evidence"]["repo"] == "/tmp/repo"
    assert decoded["findings"][0]["claim"]["kind"] == "pointer"
    assert decoded["scope_receipt"]["files_read"] == ["src/app.py"]


def test_parsed_transcript_to_dict() -> None:
    transcript = ParsedTranscript(
        source_format="generic",
        transcript_path="sample.jsonl",
        cwd="/workspace",
        assistant_messages=[AssistantMessage(index=0, text="Done.", is_final=True)],
        final_text="Done.",
    )
    d = transcript.to_dict()
    assert d["final_text"] == "Done."
    assert d["assistant_messages"][0]["is_final"] is True

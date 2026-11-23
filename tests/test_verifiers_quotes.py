"""Unit tests for quoted code and command-output verification."""

from __future__ import annotations

from handoff_verify.extractors import extract_claims
from handoff_verify.models import (
    AssistantMessage,
    CommandEvent,
    FileEditEvent,
    FileReadEvent,
    ParsedTranscript,
)
from handoff_verify.verifiers.quotes import verify_quotes


def _make_transcript(
    final_text: str,
    *,
    file_reads: list[FileReadEvent] | None = None,
    file_edits: list[FileEditEvent] | None = None,
    commands: list[CommandEvent] | None = None,
) -> ParsedTranscript:
    return ParsedTranscript(
        cwd="/repo",
        file_reads=file_reads or [],
        file_edits=file_edits or [],
        commands=commands or [],
        assistant_messages=[AssistantMessage(index=0, text=final_text, is_final=True)],
        final_text=final_text,
    )


def test_quote_exact_and_whitespace_normalized_matches() -> None:
    content = (
        "def authenticate(user, secret):\n"
        "    return verify_token(\n"
        "        user,\n"
        "        secret,\n"
        "    )\n"
    )
    text = (
        "In `src/auth.py:2`, it calls `verify_token(user, secret)`.\n\n"
        "Excerpt from `src/auth.py:1-5`:\n"
        "```python\n"
        "1→def authenticate(user, secret):\n"
        "2→  return verify_token(\n"
        "3→      user,\n"
        "4→      secret,\n"
        "5→  )\n"
        "```\n"
    )
    tr = _make_transcript(
        text,
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/auth.py",
                start_line=1,
                end_line=5,
                content=content,
            )
        ],
    )
    claims = extract_claims(tr)
    quote_claims = [c for c in claims if c.kind == "quote"]
    assert len(quote_claims) == 2
    assert verify_quotes(claims, tr) == []


def test_quote_from_file_edit_and_command_output() -> None:
    text = (
        "In `src/auth.py`, the updated check uses `return check_signature(token, key)`.\n"
        "The test suite failed with `AssertionError: expected 200, got 403`."
    )
    tr = _make_transcript(
        text,
        file_edits=[
            FileEditEvent(
                event_index=0,
                tool_name="Edit",
                file_path="src/auth.py",
                operation="edit",
                old_text="return False",
                new_text="return check_signature(token, key)",
            )
        ],
        commands=[
            CommandEvent(
                event_index=1,
                tool_name="Bash",
                command="pytest",
                stdout="FAILED test_auth.py - AssertionError: expected 200, got 403",
                exit_code=1,
                is_error=True,
            )
        ],
    )
    claims = extract_claims(tr)
    assert len([c for c in claims if c.kind == "quote"]) == 2
    assert verify_quotes(claims, tr) == []


def test_quote_fabricated_flagged_as_quote_not_found() -> None:
    text = "In `src/auth.py:2`, it calls `bypass_auth(admin_override=True)`."
    tr = _make_transcript(
        text,
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/auth.py",
                start_line=1,
                end_line=5,
                content="def authenticate(user, secret):\n    return verify_token(user, secret)\n",
            )
        ],
    )
    claims = extract_claims(tr)
    findings = verify_quotes(claims, tr)
    assert len(findings) == 1
    assert findings[0].code == "QUOTE_NOT_FOUND"
    assert findings[0].severity == "error"
    assert findings[0].evidence["claimed_file"] == "src/auth.py"


def test_quote_wrong_file_flagged_as_quote_mismatch() -> None:
    text = "In `src/auth.py:2`, it calls `compute_hash(payload, salt)`."
    tr = _make_transcript(
        text,
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/auth.py",
                start_line=1,
                end_line=3,
                content="def authenticate(user):\n    return True\n",
            ),
            FileReadEvent(
                event_index=1,
                tool_name="Read",
                file_path="src/crypto.py",
                start_line=1,
                end_line=3,
                content="def hash_it(payload, salt):\n    return compute_hash(payload, salt)\n",
            ),
        ],
    )
    claims = extract_claims(tr)
    findings = verify_quotes(claims, tr)
    assert len(findings) == 1
    assert findings[0].code == "QUOTE_MISMATCH"
    assert findings[0].severity == "error"
    assert findings[0].evidence["claimed_file"] == "src/auth.py"
    assert findings[0].evidence["observed_in_file"] == "src/crypto.py"


def test_quote_fabricated_command_output_flagged() -> None:
    text = "The command output printed `Database migration applied: 12 tables updated`."
    tr = _make_transcript(
        text,
        commands=[
            CommandEvent(
                event_index=0,
                tool_name="Bash",
                command="alembic upgrade head",
                stdout="Nothing to upgrade.",
                exit_code=0,
            )
        ],
    )
    claims = extract_claims(tr)
    findings = verify_quotes(claims, tr)
    assert len(findings) == 1
    assert findings[0].code == "QUOTE_NOT_FOUND"
    assert findings[0].severity == "error"

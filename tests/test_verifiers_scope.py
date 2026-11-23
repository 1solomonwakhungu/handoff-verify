"""Unit tests for absence & completeness scope verification."""

from __future__ import annotations

from handoff_verify.extractors import extract_claims
from handoff_verify.models import (
    AssistantMessage,
    FileReadEvent,
    PaginationEvent,
    ParsedTranscript,
    SearchEvent,
)
from handoff_verify.pipeline import build_scope_receipt
from handoff_verify.verifiers.scope import verify_scope


def _run_scope(transcript: ParsedTranscript):
    receipt = build_scope_receipt(transcript)
    claims = extract_claims(transcript)
    return verify_scope(claims, transcript, receipt)


def test_scope_quiet_when_no_absence_or_completeness_claims() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                query="foo",
                search_path="src/auth",
                excluded_dirs=["legacy"],
                truncated=True,
                truncation_reason="limit reached",
            )
        ],
        paginations=[
            PaginationEvent(
                event_index=1,
                tool_name="list_issues",
                resource="issues",
                page=1,
                total_pages=3,
                has_more=True,
                next_cursor="cur_2",
                cursor_followed=False,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Checked the first page of issues and ran a partial grep in `src/auth`.",
                is_final=True,
            )
        ],
    )
    assert _run_scope(tr) == []


def test_scope_no_search_evidence() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0, tool_name="Read", file_path="src/app.py", start_line=1, end_line=10
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Checked `src/app.py:1-10`; there are no other callers in the codebase.",
                is_final=True,
            )
        ],
    )
    findings = _run_scope(tr)
    assert len(findings) == 1
    assert findings[0].code == "NO_SEARCH_EVIDENCE"
    assert findings[0].severity == "error"


def test_scope_narrow_subdir_vs_honest_scoped_claim() -> None:
    search = SearchEvent(
        event_index=0,
        tool_name="Grep",
        query="legacy_fn",
        search_path="src/auth",
        search_paths=["src/auth"],
        match_count=0,
    )

    # Dishonest repo-wide claim -> NARROW_SEARCH_SCOPE error
    tr_wide = ParsedTranscript(
        cwd="/repo",
        searches=[search],
        assistant_messages=[
            AssistantMessage(index=0, text="Found no other callers of `legacy_fn`.", is_final=True)
        ],
    )
    findings_wide = _run_scope(tr_wide)
    assert any(f.code == "NARROW_SEARCH_SCOPE" and f.severity == "error" for f in findings_wide)

    # Honest scoped claim matching searched dir -> CLEAN
    tr_scoped = ParsedTranscript(
        cwd="/repo",
        searches=[search],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Found no other callers of `legacy_fn` in `src/auth`.",
                is_final=True,
            )
        ],
    )
    assert _run_scope(tr_scoped) == []

    # Scoped to a DIFFERENT dir (`src/billing`) than searched (`src/auth`) -> NARROW_SEARCH_SCOPE
    tr_wrong_dir = ParsedTranscript(
        cwd="/repo",
        searches=[search],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Found no other callers in `src/billing`.",
                is_final=True,
            )
        ],
    )
    findings_wrong = _run_scope(tr_wrong_dir)
    assert any(f.code == "NARROW_SEARCH_SCOPE" and f.severity == "error" for f in findings_wrong)


def test_scope_repo_root_search_is_clean() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                query="legacy_fn",
                search_path="/repo",
                search_paths=["/repo"],
                match_count=0,
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="There are no other callers anywhere.", is_final=True)
        ],
    )
    assert _run_scope(tr) == []


def test_scope_truncated_search_and_resolution_by_later_search() -> None:
    tr_trunc = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                query="verify",
                search_path=".",
                truncated=True,
                truncation_reason="result limit (20) reached",
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Checked all occurrences of `verify`.", is_final=True)
        ],
    )
    findings = _run_scope(tr_trunc)
    assert any(f.code == "TRUNCATED_SEARCH_OUTPUT" and f.severity == "error" for f in findings)

    # Later untruncated search of same query & scope resolves the truncation
    tr_resolved = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                query="verify",
                search_path="src",
                truncated=True,
                truncation_reason="result limit (20) reached",
            ),
            SearchEvent(
                event_index=1,
                tool_name="Grep",
                query="verify",
                search_path=".",
                truncated=False,
            ),
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Checked all occurrences of `verify`.", is_final=True)
        ],
    )
    assert _run_scope(tr_resolved) == []


def test_scope_unread_pagination_cursor_vs_followed() -> None:
    tr_unread = ParsedTranscript(
        cwd="/repo",
        paginations=[
            PaginationEvent(
                event_index=0,
                tool_name="mcp__github__list_issues",
                resource="issues",
                page=1,
                total_pages=3,
                has_more=True,
                next_cursor="cur_page_2",
                cursor_followed=False,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Reviewed all pages of issues and found no other issues.",
                is_final=True,
            )
        ],
    )
    findings = _run_scope(tr_unread)
    assert len(findings) == 1
    assert findings[0].code == "UNREAD_PAGINATION_CURSOR"
    assert findings[0].severity == "error"
    assert "cur_page_2" in (findings[0].suggestion or "")

    # Followed cursor -> CLEAN
    tr_followed = ParsedTranscript(
        cwd="/repo",
        paginations=[
            PaginationEvent(
                event_index=0,
                tool_name="mcp__github__list_issues",
                resource="issues",
                page=1,
                total_pages=2,
                has_more=True,
                next_cursor="cur_page_2",
                cursor_followed=True,
            ),
            PaginationEvent(
                event_index=1,
                tool_name="mcp__github__list_issues",
                resource="issues",
                page=2,
                total_pages=2,
                has_more=False,
                cursor_followed=False,
            ),
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Reviewed all pages of issues.", is_final=True)
        ],
    )
    assert _run_scope(tr_followed) == []


def test_scope_excluded_dirs_trivial_vs_nontrivial_and_acknowledged() -> None:
    # Trivial excluded dirs -> CLEAN
    tr_trivial = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                query="foo",
                search_path=".",
                excluded_dirs=["node_modules/**", ".git", "__pycache__"],
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Found no matches anywhere.", is_final=True)
        ],
    )
    assert _run_scope(tr_trivial) == []

    # Non-trivial excluded dir -> WARNING
    tr_nontrivial = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                query="foo",
                search_path=".",
                excluded_dirs=["legacy/**"],
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Found no matches anywhere.", is_final=True)
        ],
    )
    findings = _run_scope(tr_nontrivial)
    assert len(findings) == 1
    assert findings[0].code == "EXCLUDED_SEARCH_DIRS"
    assert findings[0].severity == "warning"

    # Acknowledged exclusion -> CLEAN
    tr_ack = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                query="foo",
                search_path=".",
                excluded_dirs=["legacy/**"],
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Found no matches anywhere outside `legacy`.",
                is_final=True,
            )
        ],
    )
    assert _run_scope(tr_ack) == []


def test_scope_file_scoped_absence_satisfied_by_full_read() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/app.py",
                start_line=1,
                end_line=20,
                truncated=False,
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="There are no TODOs in `src/app.py`.", is_final=True)
        ],
    )
    assert _run_scope(tr) == []

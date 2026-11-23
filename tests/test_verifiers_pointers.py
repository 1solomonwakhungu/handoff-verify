"""Unit tests for file pointer verification (read coverage, line windows, staleness, live repo)."""

from __future__ import annotations

from pathlib import Path

from handoff_verify.extractors import extract_claims
from handoff_verify.models import (
    AssistantMessage,
    FileEditEvent,
    FileReadEvent,
    ParsedTranscript,
    SearchEvent,
)
from handoff_verify.verifiers.pointers import verify_pointers


def _make_transcript(
    final_text: str,
    *,
    cwd: str = "/repo",
    file_reads: list[FileReadEvent] | None = None,
    file_edits: list[FileEditEvent] | None = None,
    searches: list[SearchEvent] | None = None,
) -> ParsedTranscript:
    return ParsedTranscript(
        cwd=cwd,
        file_reads=file_reads or [],
        file_edits=file_edits or [],
        searches=searches or [],
        assistant_messages=[AssistantMessage(index=0, text=final_text, is_final=True)],
        final_text=final_text,
    )


def test_pointer_clean_multi_window_and_cwd_normalization() -> None:
    tr = _make_transcript(
        "Verified `src/auth/login.py:40-60` and `login.py:10`.",
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="/repo/src/auth/login.py",
                start_line=1,
                end_line=50,
                line_count=50,
            ),
            FileReadEvent(
                event_index=1,
                tool_name="Read",
                file_path="src/auth/login.py",
                start_line=51,
                end_line=100,
                line_count=50,
            ),
        ],
    )
    claims = extract_claims(tr)
    findings = verify_pointers(claims, tr)
    assert findings == []


def test_pointer_unread_file_and_glob_only_with_line() -> None:
    tr = _make_transcript(
        "Found `src/listed.py`, bug at `src/listed.py:42`, and checked `src/never_seen.py`.",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Glob",
                matched_files=["src/listed.py"],
            )
        ],
    )
    claims = extract_claims(tr)
    findings = verify_pointers(claims, tr)
    codes_by_file = {(f.claim.file_path, f.code) for f in findings if f.claim}
    assert ("src/listed.py", "UNREAD_FILE") in codes_by_file
    assert ("src/never_seen.py", "UNREAD_FILE") in codes_by_file
    assert all(f.severity == "error" for f in findings)


def test_pointer_bare_file_in_glob_without_line_is_clean() -> None:
    tr = _make_transcript(
        "Matched files include `src/a.py` and `src/b.py`.",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Glob",
                matched_files=["src/a.py", "src/b.py"],
            )
        ],
    )
    claims = extract_claims(tr)
    assert verify_pointers(claims, tr) == []


def test_pointer_grep_content_line_satisfies_line_claim() -> None:
    tr = _make_transcript(
        "Found caller at `src/auth.py:42`.",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                matched_files=["src/auth.py"],
                output="src/auth.py:42:    verify_token(user, secret)",
            )
        ],
    )
    claims = extract_claims(tr)
    assert verify_pointers(claims, tr) == []


def test_pointer_line_out_of_read_window_and_disjoint_gap() -> None:
    tr = _make_transcript(
        "See `src/app.py:120` and `src/app.py:45-80`.",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/app.py",
                start_line=1,
                end_line=50,
                line_count=50,
            )
        ],
    )
    claims = extract_claims(tr)
    findings = verify_pointers(claims, tr)
    assert len(findings) == 2
    assert all(f.code == "LINE_OUT_OF_READ_WINDOW" and f.severity == "error" for f in findings)
    suggestions = [f.suggestion or "" for f in findings]
    assert any("51-120" in s for s in suggestions)
    assert any("51-80" in s for s in suggestions)


def test_pointer_stale_read_after_edit_vs_clean_reread_and_write() -> None:
    # 1. Read -> multi-line Edit -> no re-read -> cite line 42 => STALE_FILE_READ (error)
    tr_stale = _make_transcript(
        "In `src/app.py:42`, it validates the request.",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/app.py",
                start_line=1,
                end_line=50,
                line_count=50,
            )
        ],
        file_edits=[
            FileEditEvent(
                event_index=1,
                tool_name="Edit",
                file_path="src/app.py",
                operation="edit",
                old_text="x = 1",
                new_text="x = 1\ny = 2\nz = 3",
            )
        ],
    )
    findings_stale = verify_pointers(extract_claims(tr_stale), tr_stale)
    assert any(f.code == "STALE_FILE_READ" and f.severity == "error" for f in findings_stale)

    # 2. Read -> Edit -> Re-read => CLEAN
    tr_reread = _make_transcript(
        "In `src/app.py:42`, it validates the request.",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/app.py",
                start_line=1,
                end_line=50,
                line_count=50,
            ),
            FileReadEvent(
                event_index=2,
                tool_name="Read",
                file_path="src/app.py",
                start_line=1,
                end_line=55,
                line_count=55,
            ),
        ],
        file_edits=[
            FileEditEvent(
                event_index=1,
                tool_name="Edit",
                file_path="src/app.py",
                operation="edit",
                old_text="x = 1",
                new_text="x = 1\ny = 2\nz = 3",
            )
        ],
    )
    assert verify_pointers(extract_claims(tr_reread), tr_reread) == []

    # 3. Full Write edit -> cite line within written content => CLEAN
    tr_write = _make_transcript(
        "Created `src/new.py:2` with the handler.",
        file_edits=[
            FileEditEvent(
                event_index=0,
                tool_name="Write",
                file_path="src/new.py",
                operation="write",
                content="def handler():\n    return 200\n",
            )
        ],
    )
    assert verify_pointers(extract_claims(tr_write), tr_write) == []

    # 4. Bare file mention summarizing an Edit ("Updated `src/app.py`") => CLEAN
    tr_edit_summary = _make_transcript(
        "Updated `src/app.py` to add retry logic.",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/app.py",
                start_line=1,
                end_line=20,
                line_count=20,
            )
        ],
        file_edits=[
            FileEditEvent(
                event_index=1,
                tool_name="Edit",
                file_path="src/app.py",
                operation="edit",
                old_text="retries = 0",
                new_text="retries = 3",
            )
        ],
    )
    assert verify_pointers(extract_claims(tr_edit_summary), tr_edit_summary) == []


def test_pointer_live_repo_checks_not_found_out_of_bounds_and_drift(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    live_file = src_dir / "app.py"
    live_file.write_text("line1\nline2_changed\nline3\n", encoding="utf-8")

    # 1. File not found on disk
    tr_missing = _make_transcript(
        "Checked `src/ghost.py:5`.",
        cwd=str(tmp_path),
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/ghost.py",
                start_line=1,
                end_line=10,
                line_count=10,
            )
        ],
    )
    findings_missing = verify_pointers(
        extract_claims(tr_missing, repo_root=tmp_path), tr_missing, repo_root=tmp_path
    )
    assert any(f.code == "FILE_NOT_FOUND" and f.severity == "error" for f in findings_missing)

    # 2. Line out of bounds on live file (file only has 3 lines, claim cites line 10)
    tr_oob = _make_transcript(
        "Checked `src/app.py:10`.",
        cwd=str(tmp_path),
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/app.py",
                start_line=1,
                end_line=12,
                line_count=12,
            )
        ],
    )
    findings_oob = verify_pointers(
        extract_claims(tr_oob, repo_root=tmp_path), tr_oob, repo_root=tmp_path
    )
    assert any(f.code == "LINE_OUT_OF_BOUNDS" and f.severity == "error" for f in findings_oob)

    # 3. Live file content differs from transcript read (external modification)
    tr_drift = _make_transcript(
        "Checked `src/app.py:2`.",
        cwd=str(tmp_path),
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/app.py",
                start_line=1,
                end_line=3,
                line_count=3,
                content="line1\nline2_original\nline3",
            )
        ],
    )
    findings_drift = verify_pointers(
        extract_claims(tr_drift, repo_root=tmp_path), tr_drift, repo_root=tmp_path
    )
    assert any(
        f.code == "STALE_FILE_READ"
        and f.evidence.get("reason") == "live_file_differs_from_transcript_read"
        for f in findings_drift
    )

    # 4. Clean match against live non-git repo
    tr_clean = _make_transcript(
        "Checked `src/app.py:1-3`.",
        cwd=str(tmp_path),
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/app.py",
                start_line=1,
                end_line=3,
                line_count=3,
                content="line1\nline2_changed\nline3",
            )
        ],
    )
    assert (
        verify_pointers(extract_claims(tr_clean, repo_root=tmp_path), tr_clean, repo_root=tmp_path)
        == []
    )

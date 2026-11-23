"""Unit tests for handoff_verify.formatters."""

from __future__ import annotations

import json

import pytest

from handoff_verify.formatters import (
    format_hook_feedback,
    format_json,
    format_markdown,
    format_report,
    format_text,
    report_to_json_dict,
)
from handoff_verify.models import Claim, Finding, ScopeReceipt, VerificationReport


def _clean_report() -> VerificationReport:
    return VerificationReport(
        schema_version="1.0",
        transcript_path="/tmp/session.jsonl",
        source_format="claude_code",
        claims=[
            Claim(
                kind="pointer",
                text="src/app.py:10-20",
                file_path="src/app.py",
                start_line=10,
                end_line=20,
            ),
            Claim(kind="quote", text="def run():", file_path="src/app.py", snippet="def run():"),
            Claim(kind="absence", text="no other callers"),
        ],
        findings=[],
        scope_receipt=ScopeReceipt(
            files_read=["src/app.py", "src/utils.py"],
            searched_roots=["."],
        ),
        passed=True,
        duration_ms=1.23,
    )


def _anomaly_report(*, passed: bool = False) -> VerificationReport:
    c_ptr = Claim(
        kind="pointer",
        text="src/auth.py:120-135",
        file_path="src/auth.py",
        start_line=120,
        end_line=135,
    )
    c_abs = Claim(
        kind="absence",
        text="no other callers anywhere in the repo",
    )
    c_quote = Claim(
        kind="quote",
        text="`verify_token(user, secret=True)`",
        file_path="src/auth.py",
        snippet="verify_token(user, secret=True)",
    )
    findings = [
        # Intentionally out of severity order to verify sorting (error -> warning -> info).
        Finding(
            severity="info",
            code="scope_info_note",
            claim=None,
            evidence={"files_read": 2},
            message="Search covered 1 of 3 top-level directories.",
        ),
        Finding(
            severity="warning",
            code="scope_truncated_search",
            claim=c_abs,
            evidence={
                "tool": "Grep",
                "searched_roots": ["src/auth"],
                "truncated": True,
                "reason": "limit reached",
            },
            message="Claimed repo-wide absence, but grep was limited to src/auth and truncated.",
            suggestion="Re-run grep across the repo root without output truncation.",
        ),
        Finding(
            severity="error",
            code="pointer_line_out_of_window",
            claim=c_ptr,
            evidence={"observed_ranges": ["1-80"], "requested_range": "120-135"},
            message="Cited lines 120-135 in src/auth.py were outside observed read window 1-80.",
            suggestion="Read src/auth.py lines 120-135 before citing them.",
        ),
        Finding(
            severity="error",
            code="quote_mismatch",
            claim=c_quote,
            evidence={"file": "src/auth.py", "matched": False},
            message="Quoted snippet does not appear in observed reads of src/auth.py.",
            suggestion="Verify the exact signature in src/auth.py.",
        ),
    ]
    return VerificationReport(
        schema_version="1.0",
        transcript_path="/repo/.claude/session.jsonl",
        source_format="claude_code",
        claims=[c_ptr, c_abs, c_quote],
        findings=findings,
        scope_receipt=ScopeReceipt(
            searched_roots=["src/auth"],
            excluded_dirs=["vendor", "node_modules"],
            truncated_outputs=[
                {"event_index": 2, "tool_name": "Grep", "reason": "limit reached"},
            ],
            unread_cursors=[
                {
                    "event_index": 4,
                    "tool_name": "list_issues",
                    "resource": "issues",
                    "next_cursor": "cur_2",
                    "page": 1,
                    "total_pages": 3,
                }
            ],
            files_read=["src/auth.py", "src/config.py"],
            files_edited=["src/auth.py"],
        ),
        passed=passed,
        duration_ms=4.56,
    )


def test_clean_text_and_quiet_on_pass() -> None:
    report = _clean_report()

    text = format_text(report)
    assert text == "PASS: 0 findings (3 claims checked, 2 files read, 1.2ms)"
    assert "\n" not in text

    assert format_text(report, quiet_on_pass=True) == ""
    assert format_report(report, "text", quiet_on_pass=True) == ""


def test_clean_markdown_and_quiet_on_pass() -> None:
    report = _clean_report()

    md = format_markdown(report)
    assert md == "**handoff-verify:** PASS — 0 findings (3 claims checked, 2 files read, 1.2ms)"
    assert "\n" not in md

    assert format_markdown(report, quiet_on_pass=True) == ""
    assert format_report(report, "markdown", quiet_on_pass=True) == ""


def test_clean_hook_feedback_is_silent() -> None:
    report = _clean_report()
    assert format_hook_feedback(report) == ""


def test_clean_json_quiet_on_pass_dispatch() -> None:
    report = _clean_report()
    # Direct format_json always renders JSON.
    raw = format_json(report)
    parsed = json.loads(raw)
    assert parsed["passed"] is True
    assert parsed["summary"] == {
        "errors": 0,
        "warnings": 0,
        "infos": 0,
        "claims": 3,
        "findings": 0,
    }

    # format_report respects quiet_on_pass even for json.
    assert format_report(report, "json", quiet_on_pass=True) == ""


def test_format_text_anomalies_sorted_with_scope_receipt() -> None:
    report = _anomaly_report(passed=False)
    out = format_text(report)

    assert out.startswith(
        "FAIL: 4 findings (2 errors, 1 warning, 1 info; 3 claims checked, 2 files read, 4.6ms)"
    )
    # Errors come before warning, which comes before info.
    err_pos = out.index("[ERROR] pointer_line_out_of_window")
    warn_pos = out.index("[WARNING] scope_truncated_search")
    info_pos = out.index("[INFO] scope_info_note")
    assert err_pos < warn_pos < info_pos

    assert "Claim (pointer): src/auth.py:120-135" in out
    assert "Evidence: observed_ranges=[1-80]; requested_range=120-135" in out
    assert "Next step: Read src/auth.py lines 120-135 before citing them." in out
    assert "Scope receipt:" in out
    assert "Searched roots: src/auth" in out
    assert "Excluded dirs: vendor, node_modules" in out
    assert "Truncated outputs (1): [2] Grep (limit reached)" in out
    assert "Unread pagination cursors (1): [4] list_issues (issues, page 1/3, cursor=cur_2)" in out
    assert "Files edited: src/auth.py" in out


def test_format_markdown_anomalies_and_backtick_escaping() -> None:
    report = _anomaly_report(passed=False)
    md = format_markdown(report)

    assert "### handoff-verify: FAIL" in md
    assert "**Summary:** 4 findings (2 errors, 1 warning, 1 info)" in md
    assert "#### Findings" in md
    assert "- **[ERROR] `pointer_line_out_of_window`** — Cited lines 120-135" in md
    assert "- **Next step:** Read src/auth.py lines 120-135 before citing them." in md
    # Claim containing backticks is safely fenced with double backticks.
    assert "`` `verify_token(user, secret=True)` ``" in md
    assert "#### Scope Receipt" in md
    assert "- **Searched roots:** `src/auth`" in md
    assert "- **Excluded dirs:** `vendor`, `node_modules`" in md
    assert "- **Truncated outputs (1):** `[2] Grep` (limit reached)" in md
    assert "- **Unread pagination cursors (1):** `[4] list_issues`" in md
    assert "- **Files edited:** `src/auth.py`" in md


def test_warnings_only_pass_is_still_loud() -> None:
    report = VerificationReport(
        schema_version="1.0",
        claims=[Claim(kind="absence", text="no callers")],
        findings=[
            Finding(
                severity="warning",
                code="scope_narrow_search",
                claim=Claim(kind="absence", text="no callers"),
                message="Search only covered src/.",
                suggestion="Search the repo root.",
            )
        ],
        passed=True,
        duration_ms=0.8,
    )
    # Even with quiet_on_pass=True, non-empty findings must still be surfaced.
    text = format_text(report, quiet_on_pass=True)
    assert text.startswith("PASS (warnings): 1 finding (1 warning;")
    assert "[WARNING] scope_narrow_search" in text

    md = format_markdown(report, quiet_on_pass=True)
    assert "### handoff-verify: PASS (warnings)" in md

    hook = format_hook_feedback(report)
    assert hook.startswith("**handoff-verify: WARN**")


def test_format_json_schema_stability() -> None:
    report = _anomaly_report(passed=False)
    raw = format_json(report)
    data = json.loads(raw)

    assert data == report_to_json_dict(report)
    expected_keys = {
        "schema_version",
        "passed",
        "transcript_path",
        "source_format",
        "duration_ms",
        "summary",
        "claims",
        "findings",
        "scope_receipt",
    }
    assert set(data.keys()) == expected_keys
    assert data["schema_version"] == "1.0"
    assert data["passed"] is False
    assert data["transcript_path"] == "/repo/.claude/session.jsonl"
    assert data["source_format"] == "claude_code"
    assert data["duration_ms"] == 4.56
    assert data["summary"] == {
        "errors": 2,
        "warnings": 1,
        "infos": 1,
        "claims": 3,
        "findings": 4,
    }
    assert len(data["claims"]) == 3
    assert len(data["findings"]) == 4
    assert data["scope_receipt"]["searched_roots"] == ["src/auth"]
    assert data["scope_receipt"]["unread_cursors"][0]["next_cursor"] == "cur_2"


def test_format_hook_feedback_truncates_excess_findings() -> None:
    findings = [
        Finding(
            severity="error",
            code=f"err_{i}",
            claim=Claim(kind="pointer", text=f"src/f{i}.py:{i}"),
            message=f"File src/f{i}.py was not read.",
            suggestion=f"Read src/f{i}.py.",
        )
        for i in range(15)
    ]
    report = VerificationReport(
        claims=[f.claim for f in findings if f.claim is not None],
        findings=findings,
        passed=False,
        duration_ms=2.0,
    )

    feedback = format_hook_feedback(report, max_findings=5)
    assert "**handoff-verify: FAIL**" in feedback
    assert "err_0" in feedback
    assert "err_4" in feedback
    assert "err_5" not in feedback
    assert "10 more findings omitted" in feedback


def test_format_report_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="Unsupported output format"):
        format_report(_clean_report(), "xml")

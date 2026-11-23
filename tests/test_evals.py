"""Tests for the 24-case seeded evaluation suite, eval runner, and boundary phrasings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff_verify.cli import main
from handoff_verify.evals import (
    EvalSuiteError,
    _percentile_ms,
    format_eval_summary,
    resolve_evals_dir,
    run_eval_suite,
)
from handoff_verify.models import (
    AssistantMessage,
    FileReadEvent,
    PaginationEvent,
    ParsedTranscript,
    SearchEvent,
)
from handoff_verify.pipeline import verify_transcript


def test_eval_suite_24_cases_hit_all_targets() -> None:
    summary = run_eval_suite()

    assert summary.total == 24
    assert summary.clean_total == 12
    assert summary.failure_total == 12

    # Precision, recall, false positive rate, and latency targets
    assert summary.fp == 0
    assert summary.fn == 0
    assert summary.tp == 12
    assert summary.tn == 12
    assert summary.precision == 1.0
    assert summary.recall >= 0.90
    assert summary.recall == 1.0
    assert summary.false_positive_rate == 0.0
    assert summary.expected_code_match_rate == 1.0
    assert summary.passed_targets is True
    assert summary.median_ms < 50.0

    # Verify every case extracts real claims, auto-detects its declared format,
    # and matches expected outcomes and exact finding code sets.
    for result in summary.results:
        assert result.detected_format == result.expected_format, (
            f"{result.case_id}: expected format {result.expected_format}, "
            f"got {result.detected_format}"
        )
        assert result.claims_count >= 1, f"{result.case_id}: expected >=1 extracted claim"
        if result.expected_pass:
            assert result.outcome == "TN"
            assert result.findings_count == 0, (
                f"{result.case_id}: unexpected findings {result.actual_codes}"
            )
        else:
            assert result.outcome == "TP"
            assert result.codes_matched is True, (
                f"{result.case_id}: expected exact codes {result.expected_codes}, "
                f"got {result.actual_codes}"
            )

    formats = {r.detected_format for r in summary.results}
    assert formats == {"claude_code", "codex", "generic"}


def test_eval_strict_mode_differentiates_warnings(tmp_path: Path) -> None:
    # A non-trivial excluded search dir produces a warning (EXCLUDED_SEARCH_DIRS),
    # which passes in default mode and fails in --strict mode.
    case_path = tmp_path / "excluded_warn.jsonl"
    case_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "search",
                        "tool_name": "rg",
                        "query": "old_helper",
                        "search_path": ".",
                        "excluded_dirs": ["legacy"],
                        "match_count": 0,
                        "matched_files": [],
                        "output": "",
                        "truncated": False,
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "text": "There are no callers of `old_helper` anywhere in the codebase.",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "warn_case",
                        "file": "excluded_warn.jsonl",
                        "category": "scope",
                        "expected_pass": False,
                        "expected_codes": ["EXCLUDED_SEARCH_DIRS"],
                        "format": "generic",
                        "description": "Warning-only finding fails only under --strict.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    non_strict = run_eval_suite(tmp_path, strict=False)
    assert non_strict.fn == 1
    assert non_strict.passed_targets is False

    strict = run_eval_suite(tmp_path, strict=True)
    assert strict.tp == 1
    assert strict.fn == 0
    assert strict.expected_code_match_rate == 1.0
    assert strict.passed_targets is True


def test_eval_formatters_text_markdown_json() -> None:
    summary = run_eval_suite()

    text_compact = format_eval_summary(summary, "text", verbose=False)
    assert "EVAL PASS: 24 cases (12 clean, 12 planted failures)" in text_compact
    assert "Precision:" in text_compact
    assert "Recall:" in text_compact
    assert "By category:" in text_compact
    assert "Mismatches:" not in text_compact

    text_verbose = format_eval_summary(summary, "text", verbose=True)
    assert "Case results:" in text_verbose
    assert "clean_01_claude_single_read" in text_verbose
    assert "fail_12_generic_window_and_truncated_search" in text_verbose

    md_compact = format_eval_summary(summary, "markdown", verbose=False)
    assert "### handoff-verify Eval Suite: PASS" in md_compact
    assert "| Precision | 100.0%" in md_compact
    assert "#### Breakdown by Category" in md_compact
    assert "#### Mismatches" not in md_compact

    md_verbose = format_eval_summary(summary, "markdown", verbose=True)
    assert "#### All Cases" in md_verbose
    assert "`fail_08_claude_unread_pagination_cursor`" in md_verbose

    json_str = format_eval_summary(summary, "json")
    payload = json.loads(json_str)
    assert payload["schema_version"] == "1.0"
    assert payload["passed_targets"] is True
    assert payload["metrics"]["total"] == 24
    assert payload["metrics"]["precision"] == 1.0
    assert payload["metrics"]["recall"] == 1.0
    assert payload["metrics"]["fp"] == 0
    assert len(payload["results"]) == 24
    assert "pointers" in payload["by_category"]

    with pytest.raises(ValueError, match="Unsupported output format"):
        format_eval_summary(summary, "xml")


def test_percentile_ms_helper() -> None:
    assert _percentile_ms([], 95.0) == 0.0
    assert _percentile_ms([4.2], 95.0) == 4.2
    # 20 values 1..20 -> ceil(0.95 * 20) = 19th element -> 19.0
    assert _percentile_ms([float(i) for i in range(1, 21)], 95.0) == 19.0
    # 24 values 1..24 -> ceil(0.95 * 24) = 23rd element -> 23.0
    assert _percentile_ms([float(i) for i in range(1, 25)], 95.0) == 23.0


def test_eval_auto_discovery_from_unrelated_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even if an unrelated directory has its own ./evals/manifest.json, auto-discovery
    # prefers bundled/package evals rather than crashing on the user's unrelated folder.
    unrelated_evals = tmp_path / "evals"
    unrelated_evals.mkdir()
    (unrelated_evals / "manifest.json").write_text('{"unrelated": true}', encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    resolved = resolve_evals_dir(None)
    assert resolved != unrelated_evals.resolve()
    assert (resolved / "manifest.json").is_file()

    # Passing evals/cases directly also resolves to parent containing manifest.json
    assert resolve_evals_dir(resolved / "cases") == resolved

    summary = run_eval_suite()
    assert summary.total == 24
    assert summary.passed_targets is True


def test_eval_manifest_validation_errors(tmp_path: Path) -> None:
    # Missing manifest
    with pytest.raises(EvalSuiteError, match="eval manifest not found"):
        run_eval_suite(tmp_path)

    # Malformed JSON
    (tmp_path / "manifest.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(EvalSuiteError, match="failed to load eval manifest"):
        run_eval_suite(tmp_path)

    # Path traversal outside evals dir
    outside = tmp_path.parent / "escape.jsonl"
    outside.write_text('{"type": "assistant", "text": "hi"}\n', encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "bad_path",
                        "file": "../escape.jsonl",
                        "category": "pointers",
                        "expected_pass": True,
                        "expected_codes": [],
                        "format": "generic",
                        "description": "escapes root",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvalSuiteError, match="escapes evals dir"):
        run_eval_suite(tmp_path)

    # Duplicate id after whitespace stripping
    case_file = tmp_path / "case.jsonl"
    case_file.write_text('{"type": "assistant", "text": "hello"}\n', encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "dup_id",
                        "file": "case.jsonl",
                        "category": "pointers",
                        "expected_pass": True,
                        "expected_codes": [],
                        "format": "generic",
                        "description": "first",
                    },
                    {
                        "id": "dup_id ",
                        "file": "case.jsonl",
                        "category": "pointers",
                        "expected_pass": True,
                        "expected_codes": [],
                        "format": "generic",
                        "description": "second with trailing space",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvalSuiteError, match="duplicate eval case id"):
        run_eval_suite(tmp_path)

    # Contradictory clean case with non-empty expected_codes
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "bad_clean",
                        "file": "case.jsonl",
                        "category": "pointers",
                        "expected_pass": True,
                        "expected_codes": ["UNREAD_FILE"],
                        "format": "generic",
                        "description": "contradictory",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvalSuiteError, match="expected_pass=true with non-empty expected_codes"):
        run_eval_suite(tmp_path)

    # Unrecognized empty transcript
    empty_file = tmp_path / "empty.jsonl"
    empty_file.write_text("\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "empty_case",
                        "file": "empty.jsonl",
                        "category": "pointers",
                        "expected_pass": True,
                        "expected_codes": [],
                        "format": "generic",
                        "description": "empty transcript",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvalSuiteError, match="no recognizable transcript events"):
        run_eval_suite(tmp_path)


def test_eval_custom_failures_fp_fn_code_and_format_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_file = tmp_path / "clean.jsonl"
    clean_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "file_read",
                        "file_path": "src/app.py",
                        "start_line": 1,
                        "end_line": 2,
                        "content": "def main():\n    return 1",
                    }
                ),
                json.dumps({"type": "assistant", "text": "Checked `src/app.py:1-2`."}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fail_file = tmp_path / "unread.jsonl"
    fail_file.write_text(
        json.dumps({"type": "assistant", "text": "See `src/missing.py:10`."}) + "\n",
        encoding="utf-8",
    )

    # 1. False negative -> exit 1 + Markdown mismatch table
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "missed_failure",
                        "file": "clean.jsonl",
                        "category": "pointers",
                        "expected_pass": False,
                        "expected_codes": ["UNREAD_FILE"],
                        "format": "generic",
                        "description": "Intentional FN",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc_fn = main(["eval", "--cases-dir", str(tmp_path), "--format", "markdown"])
    out_fn = capsys.readouterr().out
    assert rc_fn == 1
    assert "### handoff-verify Eval Suite: FAIL" in out_fn
    assert "#### Mismatches" in out_fn
    assert "`missed_failure`" in out_fn

    # 2. False positive on clean case -> exit 1
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "false_positive",
                        "file": "unread.jsonl",
                        "category": "pointers",
                        "expected_pass": True,
                        "expected_codes": [],
                        "format": "generic",
                        "description": "Intentional FP",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc_fp = main(["eval", "--cases-dir", str(tmp_path)])
    out_fp = capsys.readouterr().out
    assert rc_fp == 1
    assert "EVAL FAIL" in out_fp
    assert "FP=1" in out_fp

    # 3. Wrong expected code (exact-set match required) -> exit 1
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "wrong_code",
                        "file": "unread.jsonl",
                        "category": "pointers",
                        "expected_pass": False,
                        "expected_codes": ["QUOTE_NOT_FOUND"],
                        "format": "generic",
                        "description": "Intentional code mismatch",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc_code = main(["eval", "--cases-dir", str(tmp_path)])
    capsys.readouterr()
    assert rc_code == 1

    # 4. Declared format mismatch -> exit 1
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "wrong_format",
                        "file": "clean.jsonl",
                        "category": "pointers",
                        "expected_pass": True,
                        "expected_codes": [],
                        "format": "claude_code",
                        "description": "Intentional format mismatch",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc_fmt = main(["eval", "--cases-dir", str(tmp_path)])
    capsys.readouterr()
    assert rc_fmt == 1


def test_boundary_phrasings_clean_and_pagination_recall() -> None:
    # 1. Cross-sentence anaphora ("I searched `src/auth` ... found there.") -> clean
    tr_anaphora = ParsedTranscript(
        source_format="claude_code",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                query="SessionCookie",
                search_path="src/auth",
                search_paths=["src/auth"],
                output="src/auth/middleware.py:7:class SessionCookie:",
                match_count=1,
                matched_files=["src/auth/middleware.py"],
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "I searched `src/auth` as requested. "
                    "No other callers of `SessionCookie` were found there."
                ),
                is_final=True,
            )
        ],
    )
    report_anaphora = verify_transcript(tr_anaphora)
    assert report_anaphora.passed is True
    assert report_anaphora.findings == []

    # 2. Cross-sentence "in this file" after full file read -> clean
    tr_this_file = ParsedTranscript(
        source_format="claude_code",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/auth/tokens.py",
                start_line=1,
                end_line=39,
                line_count=39,
                content="\n".join(f"line {i}" for i in range(1, 40)),
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "I reviewed `src/auth/tokens.py:1-39`. "
                    "There are no other uses of `hmac` in this file besides lines 26-27."
                ),
                is_final=True,
            )
        ],
    )
    report_this_file = verify_transcript(tr_this_file)
    assert report_this_file.passed is True
    assert report_this_file.findings == []

    # 3. Explicitly out-of-scope / not-opened pointer with line number -> clean
    tr_unopened = ParsedTranscript(
        source_format="claude_code",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/auth/tokens.py",
                start_line=1,
                end_line=10,
                line_count=10,
                content="\n".join(f"line {i}" for i in range(1, 11)),
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "Checked `src/auth/tokens.py:1-5`. "
                    "The caller you mentioned in `src/api/login.py:42` was out of scope "
                    "and I did not open it."
                ),
                is_final=True,
            )
        ],
    )
    report_unopened = verify_transcript(tr_unopened)
    assert report_unopened.passed is True
    assert report_unopened.findings == []

    # 4. "None of the open issues mention ..." with unread page 2 -> UNREAD_PAGINATION_CURSOR
    tr_pag = ParsedTranscript(
        source_format="claude_code",
        paginations=[
            PaginationEvent(
                event_index=0,
                tool_name="mcp__github__list_issues",
                resource="acme/payments",
                page=1,
                total_pages=3,
                has_more=True,
                next_cursor="cur_p2",
                cursor_followed=False,
                item_count=2,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="None of the open issues mention webhook timeouts.",
                is_final=True,
            )
        ],
    )
    report_pag = verify_transcript(tr_pag)
    assert report_pag.passed is False
    assert [f.code for f in report_pag.findings] == ["UNREAD_PAGINATION_CURSOR"]

"""Tests for the 24-case seeded evaluation suite and eval runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff_verify.cli import main
from handoff_verify.evals import (
    EvalSuiteError,
    format_eval_summary,
    resolve_evals_dir,
    run_eval_suite,
)
from handoff_verify.models import Claim, FileReadEvent, ParsedTranscript
from handoff_verify.verifiers.quotes import verify_quotes


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

    # Verify every case actually exercises claims, auto-detects its expected format,
    # and matches expected outcomes/codes cleanly.
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
                f"{result.case_id}: expected codes {result.expected_codes}, "
                f"got {result.actual_codes}"
            )

    # Multi-format coverage across Claude Code, Codex, and Generic JSONL
    formats = {r.detected_format for r in summary.results}
    assert formats == {"claude_code", "codex", "generic"}


def test_eval_suite_strict_mode_zero_false_positives() -> None:
    summary = run_eval_suite(strict=True)
    assert summary.passed_targets is True
    assert summary.fp == 0
    assert summary.recall == 1.0


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


def test_eval_auto_discovery_from_unrelated_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = resolve_evals_dir(None)
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

    # Contradictory clean case with non-empty expected_codes
    case_file = tmp_path / "case.jsonl"
    case_file.write_text('{"type": "assistant", "text": "hello"}\n', encoding="utf-8")
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


def test_eval_custom_missed_target_returns_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_path = tmp_path / "clean_as_fail.jsonl"
    case_path.write_text(
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
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "missed_failure",
                        "file": "clean_as_fail.jsonl",
                        "category": "pointers",
                        "expected_pass": False,
                        "expected_codes": ["UNREAD_FILE"],
                        "format": "generic",
                        "description": "Intentional FN to test failing eval target exit code.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rc = main(["eval", "--cases-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "EVAL FAIL" in captured.out
    assert "Mismatches:" in captured.out
    assert "missed_failure" in captured.out


def test_quote_operator_spacing_normalization_for_lt_gt() -> None:
    transcript = ParsedTranscript(
        source_format="generic",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/bucket.py",
                start_line=1,
                end_line=3,
                content="if cost <= 0 or cost > self.capacity:\n    raise ValueError('bad')",
            )
        ],
    )
    matching_claim = Claim(
        kind="quote",
        text="`if cost<=0 or cost>self.capacity:`",
        file_path="src/bucket.py",
        snippet="if cost<=0 or cost>self.capacity:",
        context="In `src/bucket.py:1`, checks `if cost<=0 or cost>self.capacity:`.",
    )
    inverted_claim = Claim(
        kind="quote",
        text="`if cost>=0 or cost<self.capacity:`",
        file_path="src/bucket.py",
        snippet="if cost>=0 or cost<self.capacity:",
        context="In `src/bucket.py:1`, checks `if cost>=0 or cost<self.capacity:`.",
    )

    assert verify_quotes([matching_claim], transcript) == []
    findings = verify_quotes([inverted_claim], transcript)
    assert len(findings) == 1
    assert findings[0].code == "QUOTE_NOT_FOUND"

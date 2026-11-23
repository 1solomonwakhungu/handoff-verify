"""Unit and integration tests for pipeline stub and CLI commands."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from handoff_verify import __version__
from handoff_verify.cli import main
from handoff_verify.parsers import auto_parse
from handoff_verify.pipeline import verify_transcript


def _write_sample_transcript(path: Path) -> Path:
    records = [
        {"type": "meta", "cwd": "/repo"},
        {
            "type": "file_read",
            "file_path": "src/app.py",
            "start_line": 1,
            "end_line": 10,
            "content": "def run():\n    pass",
        },
        {
            "type": "search",
            "tool_name": "grep",
            "query": "run",
            "search_path": "src",
            "excluded_dirs": ["vendor"],
            "matched_files": ["src/app.py"],
            "truncated": True,
            "truncation_reason": "limit reached",
        },
        {
            "type": "pagination",
            "tool_name": "list_issues",
            "resource": "issues",
            "page": 1,
            "total_pages": 3,
            "has_more": True,
            "next_cursor": "cur_2",
        },
        {"type": "assistant", "text": "Checked src/app.py:1-10 and found no other callers."},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_verify_transcript_builds_scope_receipt(tmp_path: Path) -> None:
    transcript_file = _write_sample_transcript(tmp_path / "session.jsonl")
    parsed = auto_parse(transcript_file)
    report = verify_transcript(parsed, repo_root=tmp_path, strict=False)

    assert report.schema_version == "1.0"
    assert report.passed is True
    assert report.claims == []
    assert report.findings == []
    assert report.duration_ms >= 0.0
    assert report.scope_receipt.files_read == ["src/app.py"]
    assert report.scope_receipt.searched_roots == ["src"]
    assert report.scope_receipt.excluded_dirs == ["vendor"]
    assert len(report.scope_receipt.truncated_outputs) == 1
    assert len(report.scope_receipt.unread_cursors) == 1
    assert report.scope_receipt.unread_cursors[0]["next_cursor"] == "cur_2"


def test_cli_version(capsys) -> None:
    rc = main(["--version"])
    assert rc == 0
    captured = capsys.readouterr()
    assert f"handoff-verify {__version__}" in captured.out


def test_cli_inspect_text_and_json(tmp_path: Path, capsys) -> None:
    transcript_file = _write_sample_transcript(tmp_path / "session.jsonl")

    rc_text = main(["inspect", str(transcript_file)])
    assert rc_text == 0
    out_text = capsys.readouterr().out
    assert "Format:     generic" in out_text
    assert "src/app.py" in out_text
    assert "Unread pagination cursors: 1" in out_text

    rc_json = main(["inspect", str(transcript_file), "--format", "json"])
    assert rc_json == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_format"] == "generic"
    assert payload["counts"]["file_reads"] == 1
    assert payload["counts"]["searches"] == 1
    assert payload["counts"]["paginations"] == 1
    assert payload["files_read"] == ["src/app.py"]


def test_cli_check_formats_and_quiet_on_pass(tmp_path: Path, capsys) -> None:
    transcript_file = _write_sample_transcript(tmp_path / "session.jsonl")

    rc_text = main(["check", str(transcript_file), "--repo", str(tmp_path)])
    assert rc_text == 0
    assert capsys.readouterr().out.startswith("PASS:")

    rc_md = main(["check", str(transcript_file), "--format", "markdown"])
    assert rc_md == 0
    assert "**handoff-verify:** PASS" in capsys.readouterr().out

    rc_json = main(["check", str(transcript_file), "--format", "json", "--strict"])
    assert rc_json == 0
    report_data = json.loads(capsys.readouterr().out)
    assert report_data["passed"] is True
    assert report_data["schema_version"] == "1.0"

    rc_quiet = main(["check", str(transcript_file), "--quiet-on-pass"])
    assert rc_quiet == 0
    assert capsys.readouterr().out == ""


def test_cli_error_exit_codes(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "does_not_exist.jsonl"
    assert main(["inspect", str(missing)]) == 2
    assert "not found" in capsys.readouterr().err

    assert main(["check", str(missing)]) == 2
    assert "not found" in capsys.readouterr().err

    transcript_file = _write_sample_transcript(tmp_path / "session.jsonl")
    assert main(["check", str(transcript_file), "--repo", str(tmp_path / "missing_dir")]) == 2
    assert "repository directory not found" in capsys.readouterr().err

    # Bad CLI usage exits 2
    assert main(["--unknown-flag"]) == 2


def test_console_entrypoint_subprocess(tmp_path: Path) -> None:
    transcript_file = _write_sample_transcript(tmp_path / "session.jsonl")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "handoff_verify.cli",
            "check",
            str(transcript_file),
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["passed"] is True

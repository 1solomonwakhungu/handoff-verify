"""CLI integration tests for `hook claude-code`, `init --claude-code`, and `check` formatters."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

import handoff_verify.cli as cli_mod
import handoff_verify.hooks as hooks_mod
from handoff_verify.cli import main
from handoff_verify.models import Claim, Finding, ScopeReceipt, VerificationReport


def _write_sample_claude_transcript(path: Path) -> Path:
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "src/app.py", "offset": 1, "limit": 20},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "     1→def run():\n     2→    return 1\n",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Verified src/app.py:1-2."}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _sample_failing_report() -> VerificationReport:
    claim = Claim(
        kind="pointer",
        text="src/missing.py:99",
        file_path="src/missing.py",
        start_line=99,
        end_line=99,
    )
    return VerificationReport(
        schema_version="1.0",
        source_format="claude_code",
        claims=[claim],
        findings=[
            Finding(
                severity="error",
                code="unread_file_pointer",
                claim=claim,
                evidence={"files_read": ["src/app.py"]},
                message="File src/missing.py was never read.",
                suggestion="Read src/missing.py around line 99.",
            )
        ],
        scope_receipt=ScopeReceipt(
            files_read=["src/app.py"],
            searched_roots=["src"],
        ),
        passed=False,
        duration_ms=2.3,
    )


def test_cli_check_uses_new_formatters_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript = _write_sample_claude_transcript(tmp_path / "session.jsonl")
    monkeypatch.setattr(
        cli_mod,
        "verify_transcript",
        lambda t, *, repo_root=None, strict=False: _sample_failing_report(),
    )

    rc_md = main(["check", str(transcript), "--format", "markdown"])
    assert rc_md == 1
    out_md = capsys.readouterr().out
    assert "### handoff-verify: FAIL" in out_md
    assert "unread_file_pointer" in out_md
    assert "#### Scope Receipt" in out_md

    rc_json = main(["check", str(transcript), "--format", "json"])
    assert rc_json == 1
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == "1.0"
    assert data["passed"] is False
    assert data["summary"]["errors"] == 1
    assert data["summary"]["claims"] == 1
    assert data["summary"]["findings"] == 1


def test_cli_hook_claude_code_advise_and_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript = _write_sample_claude_transcript(tmp_path / "subagent.jsonl")
    monkeypatch.setattr(
        hooks_mod,
        "verify_transcript",
        lambda t, *, repo_root=None, strict=False: _sample_failing_report(),
    )

    payload = json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "agent_transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "stop_hook_active": False,
        }
    )

    # Advise mode: exit 0 with structured JSON on stdout.
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc_advise = main(["hook", "claude-code", "--mode", "advise"])
    assert rc_advise == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    data = json.loads(captured.out)
    assert data["hookSpecificOutput"]["hookEventName"] == "SubagentStop"
    assert "unread_file_pointer" in data["systemMessage"]

    # Block mode: exit 2 with concise stderr receipt.
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc_block = main(["hook", "claude-code", "--mode", "block"])
    assert rc_block == 2
    captured_block = capsys.readouterr()
    assert captured_block.out == ""
    assert "unread_file_pointer" in captured_block.err


def test_cli_hook_claude_code_subprocess_clean_and_fail_open(tmp_path: Path) -> None:
    transcript = _write_sample_claude_transcript(tmp_path / "session.jsonl")
    clean_payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
        }
    )

    proc_clean = subprocess.run(
        [sys.executable, "-m", "handoff_verify.cli", "hook", "claude-code"],
        input=clean_payload,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_clean.returncode == 0
    assert proc_clean.stdout == ""
    assert proc_clean.stderr == ""

    # Fail-open on invalid JSON stdin exits 0.
    proc_open = subprocess.run(
        [sys.executable, "-m", "handoff_verify.cli", "hook", "claude-code"],
        input="not-json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_open.returncode == 0
    assert proc_open.stdout == ""
    assert "handoff-verify hook:" in proc_open.stderr

    # Fail-closed on invalid JSON stdin exits 2.
    proc_closed = subprocess.run(
        [sys.executable, "-m", "handoff_verify.cli", "hook", "claude-code", "--fail-closed"],
        input="not-json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_closed.returncode == 2
    assert "handoff-verify hook:" in proc_closed.stderr


def test_cli_init_claude_code_print_snippet(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["init", "--claude-code", "--mode", "block", "--strict"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    snippet = json.loads(captured.out)
    assert (
        snippet["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
        == "handoff-verify hook claude-code --mode block --strict"
    )
    assert (
        snippet["hooks"]["Stop"][0]["hooks"][0]["command"]
        == "handoff-verify hook claude-code --mode block --strict"
    )


def test_cli_init_claude_code_write_merge_and_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    initial = {
        "theme": "dark",
        "hooks": {
            "PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "ruff check"}]}
            ]
        },
    }
    settings_path.write_text(json.dumps(initial, indent=2), encoding="utf-8")

    # First write merges cleanly while preserving unrelated keys & hooks.
    rc1 = main(["init", "--claude-code", "--write", "--settings", str(settings_path)])
    assert rc1 == 0
    out1 = capsys.readouterr().out
    assert "Updated Claude Code hooks" in out1

    merged1 = json.loads(settings_path.read_text(encoding="utf-8"))
    assert merged1["theme"] == "dark"
    assert merged1["hooks"]["PostToolUse"] == initial["hooks"]["PostToolUse"]
    assert len(merged1["hooks"]["SubagentStop"]) == 1
    assert len(merged1["hooks"]["Stop"]) == 1

    # Second write with same flags is idempotent (no duplication).
    rc2 = main(["init", "--claude-code", "--write", "--settings", str(settings_path)])
    assert rc2 == 0
    capsys.readouterr()
    merged2 = json.loads(settings_path.read_text(encoding="utf-8"))
    assert merged2 == merged1

    # Updating to block + strict updates in place.
    rc3 = main(
        [
            "init",
            "--claude-code",
            "--write",
            "--settings",
            str(settings_path),
            "--mode",
            "block",
            "--strict",
            "--command",
            "uvx handoff-verify",
        ]
    )
    assert rc3 == 0
    capsys.readouterr()
    merged3 = json.loads(settings_path.read_text(encoding="utf-8"))
    assert len(merged3["hooks"]["SubagentStop"]) == 1
    assert len(merged3["hooks"]["Stop"]) == 1
    assert (
        merged3["hooks"]["Stop"][0]["hooks"][0]["command"]
        == "uvx handoff-verify hook claude-code --mode block --strict"
    )


def test_cli_init_claude_code_creates_missing_parent_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_path = tmp_path / "nested" / ".claude" / "settings.json"
    rc = main(["init", "--claude-code", "--write", "--settings", str(settings_path)])
    assert rc == 0
    assert "Created Claude Code hooks" in capsys.readouterr().out
    assert settings_path.is_file()


def test_cli_init_claude_code_refuses_malformed_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_settings = tmp_path / "settings.json"
    bad_settings.write_text("{not valid json", encoding="utf-8")

    rc = main(["init", "--claude-code", "--write", "--settings", str(bad_settings)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "malformed JSON" in err
    assert bad_settings.read_text(encoding="utf-8") == "{not valid json"


def test_cli_init_requires_target_flag() -> None:
    assert main(["init"]) == 2


def test_cli_hook_usage_errors_fail_open_by_default_and_fail_closed_with_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Typo / unsupported flag / unknown hook target MUST NOT exit 2 without --fail-closed
    # (exit 2 blocks every Stop in Claude Code!).
    assert main(["hook", "claude-code", "--mode", "advise", "--timeout", "5"]) == 0
    assert "unrecognized arguments" in capsys.readouterr().err

    assert main(["hook", "claude-code", "--mode", "warn"]) == 0
    assert "invalid choice" in capsys.readouterr().err

    assert main(["hook", "claude_code"]) == 0
    assert "invalid choice" in capsys.readouterr().err

    # With --fail-closed, hook usage errors exit 2.
    assert main(["hook", "claude-code", "--fail-closed", "--timeout", "5"]) == 2
    assert "unrecognized arguments" in capsys.readouterr().err

    # Non-hook usage errors still exit 2.
    assert main(["check", "--unknown-flag"]) == 2
    capsys.readouterr()


def test_cli_init_reports_actual_written_command_and_refuses_invalid_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_path = tmp_path / "settings.json"
    rc = main(
        [
            "init",
            "--claude-code",
            "--write",
            "--settings",
            str(settings_path),
            "--mode",
            "block",
            "--strict",
            "--command",
            "uvx handoff-verify hook claude-code",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "command='uvx handoff-verify hook claude-code'" in out
    assert "mode=block" not in out

    # Invalid UTF-8 settings.json exits 2 with clean Error message (not "internal failure").
    bad_utf8 = tmp_path / "bad_utf8.json"
    bad_utf8.write_bytes(b"\xff\xfe\x00")
    rc_bad = main(["init", "--claude-code", "--write", "--settings", str(bad_utf8)])
    assert rc_bad == 2
    err = capsys.readouterr().err
    assert "Error: failed to read" in err
    assert "internal failure" not in err

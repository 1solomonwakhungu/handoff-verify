"""End-to-end subprocess smoke tests for the handoff-verify CLI and hooks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from handoff_verify import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
EVALS_CASES = REPO_ROOT / "evals" / "cases"


def _cli_cmd(*args: str) -> list[str]:
    script = Path(sys.executable).with_name("handoff-verify")
    if script.is_file():
        return [str(script), *args]
    return [sys.executable, "-m", "handoff_verify.cli", *args]


def _run_cli(
    *args: str,
    stdin: str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _cli_cmd(*args),
        input=stdin,
        text=True,
        capture_output=True,
        cwd=str(cwd or REPO_ROOT),
        check=False,
    )


def test_e2e_version() -> None:
    proc = _run_cli("--version")
    assert proc.returncode == 0
    assert f"handoff-verify {__version__}" in proc.stdout


def test_e2e_inspect_clean_case_text_and_json() -> None:
    case = EVALS_CASES / "clean_01_claude_single_read.jsonl"

    proc_text = _run_cli("inspect", str(case))
    assert proc_text.returncode == 0
    assert "Format:     claude_code" in proc_text.stdout
    assert "src/auth/tokens.py" in proc_text.stdout

    proc_json = _run_cli("inspect", str(case), "--format", "json")
    assert proc_json.returncode == 0
    payload = json.loads(proc_json.stdout)
    assert payload["source_format"] == "claude_code"
    assert payload["counts"]["file_reads"] == 1
    assert payload["counts"]["malformed_lines"] == 0


def test_e2e_check_clean_case_and_quiet_on_pass() -> None:
    case = EVALS_CASES / "clean_02_claude_multi_window_read.jsonl"

    proc = _run_cli("check", str(case))
    assert proc.returncode == 0
    assert proc.stdout.startswith("PASS: 0 findings")
    assert proc.stderr == ""

    proc_quiet = _run_cli("check", str(case), "--quiet-on-pass")
    assert proc_quiet.returncode == 0
    assert proc_quiet.stdout == ""
    assert proc_quiet.stderr == ""


def test_e2e_check_failure_case_all_formats() -> None:
    case = EVALS_CASES / "fail_01_claude_unread_pointer.jsonl"

    proc_text = _run_cli("check", str(case), "--format", "text")
    assert proc_text.returncode == 1
    assert "FAIL:" in proc_text.stdout
    assert "UNREAD_FILE" in proc_text.stdout
    assert "src/auth/session_store.py" in proc_text.stdout
    assert "Next step:" in proc_text.stdout

    proc_md = _run_cli("check", str(case), "--format", "markdown")
    assert proc_md.returncode == 1
    assert "### handoff-verify: FAIL" in proc_md.stdout
    assert "`UNREAD_FILE`" in proc_md.stdout

    proc_json = _run_cli("check", str(case), "--format", "json")
    assert proc_json.returncode == 1
    report = json.loads(proc_json.stdout)
    assert report["schema_version"] == "1.0"
    assert report["passed"] is False
    assert report["summary"]["errors"] == 1
    assert report["findings"][0]["code"] == "UNREAD_FILE"


def test_e2e_claude_code_hook_advise_clean_and_failing() -> None:
    clean_case = EVALS_CASES / "clean_01_claude_single_read.jsonl"
    clean_payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(clean_case),
            "stop_hook_active": False,
        }
    )
    proc_clean = _run_cli("hook", "claude-code", "--mode", "advise", stdin=clean_payload)
    assert proc_clean.returncode == 0
    assert proc_clean.stdout == ""
    assert proc_clean.stderr == ""

    fail_case = EVALS_CASES / "fail_01_claude_unread_pointer.jsonl"
    fail_payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(fail_case),
            "stop_hook_active": False,
        }
    )
    proc_fail = _run_cli("hook", "claude-code", "--mode", "advise", stdin=fail_payload)
    assert proc_fail.returncode == 0
    assert proc_fail.stderr == ""
    out = json.loads(proc_fail.stdout)
    assert "UNREAD_FILE" in out["systemMessage"]
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "UNREAD_FILE" in out["hookSpecificOutput"]["additionalContext"]


def test_e2e_claude_code_hook_block_and_loop_guard() -> None:
    subagent_fail = EVALS_CASES / "fail_11_claude_subagent_fabricated.jsonl"
    parent_clean = EVALS_CASES / "clean_01_claude_single_read.jsonl"

    # First stop attempt: blocks with exit 2 and loud stderr receipt for subagent transcript
    payload_first = json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "transcript_path": str(parent_clean),
            "agent_transcript_path": str(subagent_fail),
            "stop_hook_active": False,
        }
    )
    proc_block = _run_cli("hook", "claude-code", "--mode", "block", stdin=payload_first)
    assert proc_block.returncode == 2
    assert proc_block.stdout == ""
    assert "UNREAD_FILE" in proc_block.stderr
    assert "QUOTE_NOT_FOUND" in proc_block.stderr

    # Second stop attempt with stop_hook_active=true: NEVER blocks again, no additionalContext
    payload_second = json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "transcript_path": str(parent_clean),
            "agent_transcript_path": str(subagent_fail),
            "stop_hook_active": True,
        }
    )
    proc_guard = _run_cli("hook", "claude-code", "--mode", "block", stdin=payload_second)
    assert proc_guard.returncode == 0
    assert proc_guard.stderr == ""
    guarded = json.loads(proc_guard.stdout)
    assert "stop_hook_active" in guarded["systemMessage"]
    assert "hookSpecificOutput" not in guarded


def test_e2e_init_print_and_write(tmp_path: Path) -> None:
    proc_print = _run_cli("init", "--claude-code")
    assert proc_print.returncode == 0
    snippet = json.loads(proc_print.stdout)
    assert "SubagentStop" in snippet["hooks"]
    assert "Stop" in snippet["hooks"]

    settings_file = tmp_path / ".claude" / "settings.json"
    proc_write = _run_cli(
        "init",
        "--claude-code",
        "--write",
        "--mode",
        "block",
        "--settings",
        str(settings_file),
    )
    assert proc_write.returncode == 0
    assert "Created Claude Code hooks" in proc_write.stdout
    written = json.loads(settings_file.read_text(encoding="utf-8"))
    cmd = written["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
    assert cmd == "handoff-verify hook claude-code --mode block"


def test_e2e_eval_subcommand(tmp_path: Path) -> None:
    proc_text = _run_cli("eval")
    assert proc_text.returncode == 0
    assert "EVAL PASS: 24 cases" in proc_text.stdout
    assert "Precision:        100.0%" in proc_text.stdout
    assert "Recall:           100.0%" in proc_text.stdout

    proc_json = _run_cli("eval", "--format", "json")
    assert proc_json.returncode == 0
    payload = json.loads(proc_json.stdout)
    assert payload["passed_targets"] is True
    assert payload["metrics"]["total"] == 24
    assert payload["metrics"]["fp"] == 0
    assert payload["metrics"]["recall"] == 1.0

    # Missing cases dir returns exit 2
    proc_missing = _run_cli("eval", "--cases-dir", str(tmp_path / "does_not_exist"))
    assert proc_missing.returncode == 2
    assert "Error:" in proc_missing.stderr

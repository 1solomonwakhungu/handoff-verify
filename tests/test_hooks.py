"""Unit and integration tests for handoff_verify.hooks (Claude Code adapter & settings merge)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import handoff_verify.hooks as hooks_mod
from handoff_verify.hooks import (
    SettingsError,
    build_claude_code_hook_command,
    build_claude_code_settings_snippet,
    merge_claude_code_settings,
    parse_claude_hook_input,
    run_claude_code_hook,
    write_claude_code_settings,
)
from handoff_verify.models import Claim, Finding, ScopeReceipt, VerificationReport


def _write_claude_transcript(path: Path, *, final_text: str = "Checked src/app.py:1-5.") -> Path:
    records = [
        {
            "type": "assistant",
            "cwd": str(path.parent),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "src/app.py", "offset": 1, "limit": 10},
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
                        "tool_use_id": "toolu_1",
                        "content": "     1→def main():\n     2→    return 0\n",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": final_text}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _failing_report(transcript_path: str | None = None) -> VerificationReport:
    claim = Claim(
        kind="pointer",
        text="src/secret.py:42",
        file_path="src/secret.py",
        start_line=42,
        end_line=42,
    )
    return VerificationReport(
        transcript_path=transcript_path,
        source_format="claude_code",
        claims=[claim],
        findings=[
            Finding(
                severity="error",
                code="unread_file_pointer",
                claim=claim,
                evidence={"files_read": ["src/app.py"]},
                message="Claimed src/secret.py:42 was never read in this transcript.",
                suggestion="Read src/secret.py around line 42 before citing it.",
            )
        ],
        scope_receipt=ScopeReceipt(files_read=["src/app.py"]),
        passed=False,
        duration_ms=1.5,
    )


def _warning_only_report(strict: bool) -> VerificationReport:
    claim = Claim(kind="absence", text="no other callers")
    return VerificationReport(
        source_format="claude_code",
        claims=[claim],
        findings=[
            Finding(
                severity="warning",
                code="scope_truncated_search",
                claim=claim,
                evidence={"tool": "Grep", "reason": "limit reached"},
                message="Search output was truncated before asserting no callers.",
                suggestion="Re-run search without truncation.",
            )
        ],
        scope_receipt=ScopeReceipt(
            searched_roots=["src"],
            truncated_outputs=[{"event_index": 1, "tool_name": "Grep", "reason": "limit reached"}],
        ),
        passed=not strict,
        duration_ms=1.1,
    )


def test_parse_claude_hook_input_fields() -> None:
    raw = json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "session_id": "sess-123",
            "agent_id": "agent-7",
            "cwd": "/tmp/repo",
            "transcript_path": "/tmp/parent.jsonl",
            "agent_transcript_path": "/tmp/subagent.jsonl",
            "stop_hook_active": True,
        }
    )
    parsed = parse_claude_hook_input(raw)
    assert parsed.hook_event_name == "SubagentStop"
    assert parsed.session_id == "sess-123"
    assert parsed.agent_id == "agent-7"
    assert parsed.cwd == "/tmp/repo"
    assert parsed.transcript_path == "/tmp/parent.jsonl"
    assert parsed.agent_transcript_path == "/tmp/subagent.jsonl"
    assert parsed.stop_hook_active is True


def test_hook_clean_pass_is_silent_in_advise_and_block(tmp_path: Path) -> None:
    transcript = _write_claude_transcript(tmp_path / "session.jsonl")
    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "stop_hook_active": False,
        }
    )

    res_advise = run_claude_code_hook(payload, mode="advise")
    assert res_advise.exit_code == 0
    assert res_advise.stdout == ""
    assert res_advise.stderr == ""
    assert res_advise.blocked is False

    res_block = run_claude_code_hook(payload, mode="block")
    assert res_block.exit_code == 0
    assert res_block.stdout == ""
    assert res_block.stderr == ""
    assert res_block.blocked is False


def test_hook_subagent_stop_prefers_agent_transcript_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_transcript = _write_claude_transcript(tmp_path / "parent.jsonl", final_text="Parent ok.")
    subagent_transcript = _write_claude_transcript(
        tmp_path / "subagent.jsonl", final_text="Subagent cited src/secret.py:42."
    )

    seen_paths: list[str | None] = []
    seen_repos: list[Path | None] = []

    def fake_verify(transcript, *, repo_root=None, strict=False):
        seen_paths.append(transcript.transcript_path)
        seen_repos.append(repo_root)
        return _failing_report(transcript.transcript_path)

    monkeypatch.setattr(hooks_mod, "verify_transcript", fake_verify)

    payload = json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "session_id": "sess-1",
            "agent_id": "sub-1",
            "cwd": str(tmp_path),
            "transcript_path": str(parent_transcript),
            "agent_transcript_path": str(subagent_transcript),
            "stop_hook_active": False,
        }
    )
    res = run_claude_code_hook(payload, mode="advise")

    assert seen_paths == [str(subagent_transcript)]
    assert seen_repos == [tmp_path]
    assert res.exit_code == 0
    assert res.stderr == ""
    assert res.blocked is False

    out_json = json.loads(res.stdout)
    assert "unread_file_pointer" in out_json["systemMessage"]
    assert out_json["hookSpecificOutput"]["hookEventName"] == "SubagentStop"
    assert "src/secret.py:42" in out_json["hookSpecificOutput"]["additionalContext"]


def test_hook_subagent_stop_falls_back_to_transcript_path_when_agent_path_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_transcript = _write_claude_transcript(tmp_path / "parent.jsonl")
    seen_paths: list[str | None] = []

    def fake_verify(transcript, *, repo_root=None, strict=False):
        seen_paths.append(transcript.transcript_path)
        return VerificationReport(passed=True)

    monkeypatch.setattr(hooks_mod, "verify_transcript", fake_verify)

    payload = json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "transcript_path": str(parent_transcript),
            "cwd": str(tmp_path),
        }
    )
    res = run_claude_code_hook(payload, mode="advise")
    assert res.exit_code == 0
    assert seen_paths == [str(parent_transcript)]


def test_hook_stop_prefers_transcript_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    main_transcript = _write_claude_transcript(tmp_path / "main.jsonl")
    other_transcript = _write_claude_transcript(tmp_path / "other.jsonl")
    seen_paths: list[str | None] = []

    def fake_verify(transcript, *, repo_root=None, strict=False):
        seen_paths.append(transcript.transcript_path)
        return VerificationReport(passed=True)

    monkeypatch.setattr(hooks_mod, "verify_transcript", fake_verify)

    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(main_transcript),
            "agent_transcript_path": str(other_transcript),
        }
    )
    res = run_claude_code_hook(payload, mode="advise")
    assert res.exit_code == 0
    assert seen_paths == [str(main_transcript)]


def test_hook_advise_omits_hook_specific_output_when_event_name_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _write_claude_transcript(tmp_path / "session.jsonl")
    monkeypatch.setattr(
        hooks_mod,
        "verify_transcript",
        lambda t, *, repo_root=None, strict=False: _failing_report(t.transcript_path),
    )

    payload = json.dumps({"transcript_path": str(transcript)})
    res = run_claude_code_hook(payload, mode="advise")
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert "systemMessage" in data
    assert "hookSpecificOutput" not in data


def test_hook_block_mode_exits_2_with_stderr_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _write_claude_transcript(tmp_path / "session.jsonl")
    monkeypatch.setattr(
        hooks_mod,
        "verify_transcript",
        lambda t, *, repo_root=None, strict=False: _failing_report(t.transcript_path),
    )

    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        }
    )
    res = run_claude_code_hook(payload, mode="block")
    assert res.exit_code == 2
    assert res.blocked is True
    assert res.stdout == ""
    assert "unread_file_pointer" in res.stderr
    assert "Read src/secret.py around line 42" in res.stderr


def test_hook_block_mode_never_blocks_when_stop_hook_active_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _write_claude_transcript(tmp_path / "session.jsonl")
    monkeypatch.setattr(
        hooks_mod,
        "verify_transcript",
        lambda t, *, repo_root=None, strict=False: _failing_report(t.transcript_path),
    )

    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "stop_hook_active": True,
        }
    )
    res = run_claude_code_hook(payload, mode="block")
    assert res.exit_code == 0
    assert res.blocked is False
    assert res.stderr == ""

    data = json.loads(res.stdout)
    assert "stop_hook_active" in data["systemMessage"]
    assert "not blocking again" in data["systemMessage"]
    assert data["hookSpecificOutput"]["hookEventName"] == "Stop"


def test_hook_block_mode_respects_strict_for_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _write_claude_transcript(tmp_path / "session.jsonl")
    monkeypatch.setattr(
        hooks_mod,
        "verify_transcript",
        lambda t, *, repo_root=None, strict=False: _warning_only_report(strict),
    )

    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        }
    )

    # Without --strict: warning-only report passes, so block mode advises instead of blocking.
    res_non_strict = run_claude_code_hook(payload, mode="block", strict=False)
    assert res_non_strict.exit_code == 0
    assert res_non_strict.blocked is False
    assert "scope_truncated_search" in json.loads(res_non_strict.stdout)["systemMessage"]

    # With --strict: warning fails verification, so block mode exits 2.
    res_strict = run_claude_code_hook(payload, mode="block", strict=True)
    assert res_strict.exit_code == 2
    assert res_strict.blocked is True
    assert "scope_truncated_search" in res_strict.stderr


def test_hook_repo_override_takes_precedence_over_payload_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _write_claude_transcript(tmp_path / "session.jsonl")
    payload_cwd = tmp_path / "payload_cwd"
    explicit_repo = tmp_path / "explicit_repo"
    payload_cwd.mkdir()
    explicit_repo.mkdir()

    seen_repos: list[Path | None] = []

    def fake_verify(t, *, repo_root=None, strict=False):
        seen_repos.append(repo_root)
        return VerificationReport(passed=True)

    monkeypatch.setattr(hooks_mod, "verify_transcript", fake_verify)

    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "cwd": str(payload_cwd),
        }
    )
    res = run_claude_code_hook(payload, repo_root=explicit_repo)
    assert res.exit_code == 0
    assert seen_repos == [explicit_repo]


def test_hook_fail_open_vs_fail_closed_on_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Invalid JSON on stdin
    res_open = run_claude_code_hook("{not valid json", fail_closed=False)
    assert res_open.exit_code == 0
    assert res_open.stdout == ""
    assert "malformed JSON" in res_open.stderr

    res_closed = run_claude_code_hook("{not valid json", fail_closed=True)
    assert res_closed.exit_code == 2
    assert "malformed JSON" in res_closed.stderr

    # 2. Empty stdin
    assert run_claude_code_hook("", fail_closed=False).exit_code == 0
    assert run_claude_code_hook("", fail_closed=True).exit_code == 2

    # 3. Non-object JSON
    assert run_claude_code_hook("[1, 2, 3]", fail_closed=False).exit_code == 0
    assert run_claude_code_hook("[1, 2, 3]", fail_closed=True).exit_code == 2

    # 4. Missing transcript file
    missing_payload = json.dumps({"transcript_path": str(tmp_path / "missing.jsonl")})
    res_missing_open = run_claude_code_hook(missing_payload, fail_closed=False)
    assert res_missing_open.exit_code == 0
    assert "not found" in res_missing_open.stderr
    assert run_claude_code_hook(missing_payload, fail_closed=True).exit_code == 2

    # 5. Empty / unrecognized transcript file
    empty_file = tmp_path / "empty.jsonl"
    empty_file.write_text("not json at all\n", encoding="utf-8")
    empty_payload = json.dumps({"transcript_path": str(empty_file)})
    res_empty_open = run_claude_code_hook(empty_payload, fail_closed=False)
    assert res_empty_open.exit_code == 0
    assert "empty or unrecognized transcript" in res_empty_open.stderr
    assert run_claude_code_hook(empty_payload, fail_closed=True).exit_code == 2

    # 6. Unexpected internal crash inside verify_transcript
    valid_transcript = _write_claude_transcript(tmp_path / "ok.jsonl")
    valid_payload = json.dumps({"transcript_path": str(valid_transcript)})

    def boom(*args, **kwargs):
        raise RuntimeError("simulated verifier explosion")

    monkeypatch.setattr(hooks_mod, "verify_transcript", boom)
    res_crash_open = run_claude_code_hook(valid_payload, fail_closed=False)
    assert res_crash_open.exit_code == 0
    assert "simulated verifier explosion" in res_crash_open.stderr
    res_crash_closed = run_claude_code_hook(valid_payload, fail_closed=True)
    assert res_crash_closed.exit_code == 2
    assert "simulated verifier explosion" in res_crash_closed.stderr


def test_build_claude_code_hook_command_and_snippet() -> None:
    assert (
        build_claude_code_hook_command(mode="advise", strict=False)
        == "handoff-verify hook claude-code --mode advise"
    )
    assert (
        build_claude_code_hook_command(mode="block", strict=True)
        == "handoff-verify hook claude-code --mode block --strict"
    )
    assert (
        build_claude_code_hook_command(mode="block", strict=False, command="uvx handoff-verify")
        == "uvx handoff-verify hook claude-code --mode block"
    )
    assert (
        build_claude_code_hook_command(
            mode="advise",
            command="uvx handoff-verify hook claude-code --mode block --fail-closed",
        )
        == "uvx handoff-verify hook claude-code --mode block --fail-closed"
    )

    snippet = build_claude_code_settings_snippet(mode="advise")
    assert set(snippet["hooks"].keys()) == {"SubagentStop", "Stop"}
    for event in ("SubagentStop", "Stop"):
        assert snippet["hooks"][event] == [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "handoff-verify hook claude-code --mode advise",
                    }
                ]
            }
        ]


def test_merge_claude_code_settings_preserves_existing_and_is_idempotent() -> None:
    existing = {
        "permissions": {"allow": ["Bash(pytest:*)"]},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo pre"}],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "notify-done"},
                    ]
                }
            ],
        },
    }

    first = merge_claude_code_settings(existing, mode="advise")
    # Unrelated keys and hooks preserved.
    assert first["permissions"] == {"allow": ["Bash(pytest:*)"]}
    assert first["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    assert first["hooks"]["Stop"][0]["hooks"] == [{"type": "command", "command": "notify-done"}]
    assert len(first["hooks"]["Stop"]) == 2
    assert len(first["hooks"]["SubagentStop"]) == 1

    # Running again with same args is strictly idempotent.
    second = merge_claude_code_settings(first, mode="advise")
    assert second == first

    # Running with updated mode/strict updates existing entry in place (no duplicate).
    third = merge_claude_code_settings(second, mode="block", strict=True)
    assert len(third["hooks"]["Stop"]) == 2
    assert len(third["hooks"]["SubagentStop"]) == 1
    assert (
        third["hooks"]["Stop"][1]["hooks"][0]["command"]
        == "handoff-verify hook claude-code --mode block --strict"
    )
    assert (
        third["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
        == "handoff-verify hook claude-code --mode block --strict"
    )


def test_merge_deduplicates_preexisting_duplicate_handoff_verify_entries() -> None:
    duplicated = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "handoff-verify hook claude-code --mode advise",
                            "timeout": 15,
                        }
                    ]
                },
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "uvx handoff-verify hook claude-code --mode block",
                        }
                    ]
                },
            ]
        }
    }
    merged = merge_claude_code_settings(duplicated, mode="block")
    assert len(merged["hooks"]["Stop"]) == 1
    handler = merged["hooks"]["Stop"][0]["hooks"][0]
    assert handler["command"] == "handoff-verify hook claude-code --mode block"
    assert handler["timeout"] == 15


def test_write_claude_code_settings_creates_and_refuses_malformed(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    merged, created = write_claude_code_settings(settings_path, mode="advise")
    assert created is True
    assert settings_path.is_file()
    assert json.loads(settings_path.read_text(encoding="utf-8")) == merged

    # Second write updates in place.
    merged2, created2 = write_claude_code_settings(settings_path, mode="block")
    assert created2 is False
    assert (
        merged2["hooks"]["Stop"][0]["hooks"][0]["command"]
        == "handoff-verify hook claude-code --mode block"
    )

    # Malformed JSON is refused without modifying disk.
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{broken json", encoding="utf-8")
    with pytest.raises(SettingsError, match="malformed JSON"):
        write_claude_code_settings(bad_path, mode="advise")
    assert bad_path.read_text(encoding="utf-8") == "{broken json"

    # Non-object root is refused without modifying disk.
    list_path = tmp_path / "list.json"
    list_path.write_text("[]", encoding="utf-8")
    with pytest.raises(SettingsError, match="must be an object"):
        write_claude_code_settings(list_path, mode="advise")
    assert list_path.read_text(encoding="utf-8") == "[]"

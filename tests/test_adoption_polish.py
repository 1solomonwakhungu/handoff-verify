"""Tests for PR #6 adoption polish: 5 real-session false-alarm fixes, hook UX, doctor, plugin."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from handoff_verify import __version__
from handoff_verify.cli import main
from handoff_verify.doctor import format_doctor_text, run_doctor
from handoff_verify.hooks import run_claude_code_hook
from handoff_verify.parsers import auto_parse
from handoff_verify.pipeline import verify_transcript

REPO_ROOT = Path(__file__).resolve().parents[1]


def _bash(tool_id: str, command: str, output: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "cwd": "/repo",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": output}],
            },
        },
    ]


def _read(tool_id: str, file_path: str, content: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "cwd": "/repo",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Read",
                        "input": {"file_path": file_path},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}],
            },
        },
    ]


def _tool(tool_id: str, name: str, raw_input: dict, output: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "cwd": "/repo",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": raw_input}],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": output}],
            },
        },
    ]


def _final(text: str) -> dict:
    return {
        "type": "assistant",
        "cwd": "/repo",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _codes(tmp_path: Path, records: list[dict]) -> list[tuple[str, str]]:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    report = verify_transcript(auto_parse(path))
    return [(f.severity, f.code) for f in report.findings]


# --- 1A: Redirected output read back ---


def test_redirected_sed_and_tee_readback_ground_pointers_and_quotes(tmp_path: Path) -> None:
    # 1. `sed -n '10,20p' src/tax.py > /tmp/slice.log` then `Read /tmp/slice.log`
    records = [
        *_bash("b1", "sed -n '10,20p' src/tax.py > /tmp/slice.log", ""),
        *_read(
            "r1",
            "/tmp/slice.log",
            "     1→def compute(x):\n     2→    return round(x * 1.07, 2)\n",
        ),
        _final("`src/tax.py:10-11` returns `return round(x * 1.07, 2)`."),
    ]
    assert _codes(tmp_path, records) == []

    # 2. `sed -n '10,20p' src/tax.py | tee /tmp/slice.log` preserves exact window directly
    tee_records = [
        *_bash(
            "b1",
            "sed -n '10,20p' src/tax.py | tee /tmp/slice.log",
            "def compute(x):\n    return round(x * 1.07, 2)\n",
        ),
        _final("`src/tax.py:10-11` returns `return round(x * 1.07, 2)`."),
    ]
    assert _codes(tmp_path, tee_records) == []

    # 3. `grep -rn ... > /tmp/hits.log` then `Read /tmp/hits.log` credits grep hits & quotes
    grep_redir = [
        *_bash("b1", "grep -rn 'retry_limit' src/ > /tmp/hits.log", ""),
        *_read(
            "r1",
            "/tmp/hits.log",
            "     1→src/net.py:42:    if attempts >= retry_limit:\n",
        ),
        _final("`src/net.py:42` checks `if attempts >= retry_limit:`."),
    ]
    assert _codes(tmp_path, grep_redir) == []

    # 4. Redirected file NEVER read back -> still UNREAD_FILE + QUOTE_NOT_FOUND
    unread_redir = [
        *_bash("b1", "sed -n '10,20p' src/tax.py > /tmp/slice.log", ""),
        _final("`src/tax.py:11` returns `return round(x * 1.07, 2)`."),
    ]
    codes = {c for _, c in _codes(tmp_path, unread_redir)}
    assert "UNREAD_FILE" in codes
    assert "QUOTE_NOT_FOUND" in codes

    # 5. Redirected file read back, but citing line OUTSIDE the redirected window -> fails
    oob_redir = [
        *_bash("b1", "sed -n '10,12p' src/tax.py > /tmp/slice.log", ""),
        *_read("r1", "/tmp/slice.log", "     1→line10\n     2→line11\n     3→line12\n"),
        _final("`src/tax.py:90` handles errors."),
    ]
    assert ("error", "LINE_OUT_OF_READ_WINDOW") in _codes(tmp_path, oob_redir)

    # 6. Redirected file overwritten by explicit Write before Read -> does NOT credit old source
    overwritten_redir = [
        *_bash("b1", "sed -n '10,12p' src/tax.py > /tmp/slice.log", ""),
        *_tool("w1", "Write", {"file_path": "/tmp/slice.log", "content": "unrelated\n"}, "ok"),
        *_read("r1", "/tmp/slice.log", "     1→unrelated\n"),
        _final("`src/tax.py:11` handles errors."),
    ]
    assert ("error", "UNREAD_FILE") in _codes(tmp_path, overwritten_redir)


# --- 1B: grep -n / rg -n context & adjacent lines ---


def test_grep_context_flags_and_adjacent_lines(tmp_path: Path) -> None:
    # 1. `rg -n -C 2` emits `file-line-content` context lines; both pointers & quotes pass
    rg_ctx = [
        *_bash(
            "b1",
            "rg -n -C 2 'validate_sig' src/",
            (
                "src/sec/hmac.py-19-def verify_request(req):\n"
                "src/sec/hmac.py:20:    if not validate_sig(req.body, req.sig):\n"
                "src/sec/hmac.py-21-        raise ForbiddenError('bad sig')\n"
            ),
        ),
        _final(
            "`src/sec/hmac.py:19-21` checks signatures and raises "
            "`raise ForbiddenError('bad sig')`."
        ),
    ]
    assert _codes(tmp_path, rg_ctx) == []

    # 2. Adjacent line (±2) next to `grep -n` hit passes, while far line still fails
    grep_adj = [
        *_bash("b1", "grep -rn 'MAX_ITEMS' src/", "src/cfg.py:30:MAX_ITEMS = 100\n"),
        _final("`src/cfg.py:31` configures the batch bound."),
    ]
    assert _codes(tmp_path, grep_adj) == []

    grep_far = [
        *_bash("b1", "grep -rn 'MAX_ITEMS' src/", "src/cfg.py:30:MAX_ITEMS = 100\n"),
        _final("`src/cfg.py:150` configures the batch bound."),
    ]
    assert ("error", "UNREAD_FILE") in _codes(tmp_path, grep_far)

    # 3. Single-file `grep -n` joined with `;` credits bare `42:content` lines
    compound_grep = [
        *_bash("b1", "grep -n 'TIMEOUT' src/client.py ; echo ok", "42:TIMEOUT = 15\nok\n"),
        _final("`src/client.py:42-43` sets the timeout."),
    ]
    assert _codes(tmp_path, compound_grep) == []


# --- 1C: Parent relaying subagent citations under different path spellings ---


def test_parent_relay_subagent_and_unique_basename_matching(tmp_path: Path) -> None:
    # 1. Subagent reports full path range & bare filename; parent relays under alternate spellings
    records = [
        *_tool(
            "ag1",
            "Agent",
            {"description": "Audit auth"},
            (
                "Checked `/repo/src/auth/jwt.py:15-24` (`verify_jwt(token, key)`) "
                "and `session.py:8` (`is_revoked(sid)`)."
            ),
        ),
        *_read("r1", "/repo/src/pkg/app.py", "     1→def start():\n     2→    return True\n"),
        _final(
            "Relaying findings:\n"
            "- `jwt.py:18-22` calls `verify_jwt(token, key)`.\n"
            "- `src/auth/session.py:8` checks `is_revoked(sid)`.\n"
            "- `app.py:1-2` starts the service."
        ),
    ]
    assert _codes(tmp_path, records) == []

    # 2. Ambiguous basename across two DIFFERENT read files MUST NOT blindly match out-of-window
    ambiguous = [
        *_read("r1", "/repo/src/a/app.py", "     1→a1\n     2→a2\n"),
        *_read("r2", "/repo/src/b/app.py", "     1→b1\n     2→b2\n"),
        _final("`app.py:1` initializes the app."),
    ]
    assert ("error", "UNREAD_FILE") in _codes(tmp_path, ambiguous)


# --- 1D: Negated existence mentions ---


def test_negated_mentions_ignored_while_passive_unread_still_caught(tmp_path: Path) -> None:
    clean_negated = [
        *_read("r1", "/repo/src/app.py", "     1→def main():\n     2→    return 0\n"),
        _final(
            "Checked `src/app.py:1-2`. "
            "`src/app.py:99` does not exist, there is no `foo.py:42`, "
            "`bar.py` wasn't found, and `baz.py:10` doesn't exist."
        ),
    ]
    assert _codes(tmp_path, clean_negated) == []

    # Non-existence words in a later clause of a real citation MUST NOT suppress UNREAD_FILE
    still_caught = [
        *_read("r1", "/repo/src/app.py", "     1→def main():\n     2→    return 0\n"),
        _final("In `src/api/handler.py:42`, missing keys return 404 not found."),
    ]
    assert ("error", "UNREAD_FILE") in _codes(tmp_path, still_caught)


# --- 1E: Non-code absence phrases ---


def test_noncode_absence_phrases_ignored_while_code_usage_caught(tmp_path: Path) -> None:
    clean_prose = [
        *_read("r1", "/repo/src/app.py", "     1→def main():\n     2→    return 0\n"),
        _final(
            "Verified `src/app.py:1-2`. "
            "There is no usage limit, no usage docs needed, no open issue for this, "
            "no open PRs blocking merge, no errors, no warnings, and no changes elsewhere."
        ),
    ]
    assert _codes(tmp_path, clean_prose) == []

    # Genuine code-search absence claims MUST STILL trigger NO_SEARCH_EVIDENCE
    for claim_text in (
        "There is no usage of `legacy_auth` anywhere in the codebase.",
        "There is no other usage of `legacy_auth`.",
        "There are no usages of `legacy_auth`.",
    ):
        caught = [
            *_read("r1", "/repo/src/app.py", "     1→def main():\n     2→    return 0\n"),
            _final(claim_text),
        ]
        assert ("error", "NO_SEARCH_EVIDENCE") in _codes(tmp_path, caught), claim_text


# --- 2: Hook UX (warning-only silence + --include-warnings + fail-open exit 1 + hook race) ---


def test_hook_warning_only_silent_by_default_and_cli_include_warnings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real warning-only transcript: excluded non-trivial directory `legacy`
    transcript = tmp_path / "warn_session.jsonl"
    records = [
        *_tool(
            "g1",
            "Grep",
            {"pattern": "old_fn", "glob": "!legacy/**", "output_mode": "content"},
            "No matches found",
        ),
        _final("There are no other callers of `old_fn` anywhere in the codebase."),
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    # Confirm check sees the warning (and passes non-strict)
    report = verify_transcript(auto_parse(transcript))
    assert report.passed is True
    assert [(f.severity, f.code) for f in report.findings] == [("warning", "EXCLUDED_SEARCH_DIRS")]

    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        }
    )

    # Default hook: silent exit 0
    res_default = run_claude_code_hook(payload, mode="advise")
    assert res_default.exit_code == 0
    assert res_default.stdout == ""
    assert res_default.stderr == ""

    # With --include-warnings via CLI: emits systemMessage only, exit 0
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = main(["hook", "claude-code", "--mode", "advise", "--include-warnings"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "EXCLUDED_SEARCH_DIRS" in data["systemMessage"]
    assert "hookSpecificOutput" not in data


# --- 3: Doctor command ---


def test_doctor_checks_all_states_and_json_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Isolate HOME and CLAUDE_CONFIG_DIR so host machine config never leaks into tests
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    # 1. Fresh directory with missing binary on PATH and no hooks -> status=warn, exit 0
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    rc_fresh = main(["doctor", "--json"])
    assert rc_fresh == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "warn"
    assert data["ok"] is True
    assert data["healthy"] is False
    checks_by_name = {c["name"]: c for c in data["checks"]}
    assert checks_by_name["python_version"]["status"] == "ok"
    assert checks_by_name["binary_on_path"]["status"] == "warn"
    assert checks_by_name["claude_hooks"]["status"] == "warn"
    assert data["next_steps"]

    # 2. Create fake executable on PATH and install hooks -> READY (status=ok, exit 0)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_bin = bin_dir / "handoff-verify"
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    assert main(["init", "--write"]) == 0
    capsys.readouterr()

    rc_ready = main(["doctor"])
    assert rc_ready == 0
    out_ready = capsys.readouterr().out
    assert "READY" in out_ready
    assert "[OK] python_version" in out_ready
    assert "[OK] binary_on_path" in out_ready
    assert "[OK] claude_hooks" in out_ready

    # 3. Malformed .claude/settings.json -> ERROR (exit 1)
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text("{broken json", encoding="utf-8")
    rc_bad = main(["doctor"])
    assert rc_bad == 1
    out_bad = capsys.readouterr().out
    assert "ISSUES FOUND" in out_bad
    assert "[ERROR] claude_hooks" in out_bad

    # 4. Valid settings.json referencing a missing binary -> ERROR (exit 1)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/no/such/handoff-verify hook claude-code",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    rc_missing_exe = main(["doctor"])
    assert rc_missing_exe == 1
    assert "not on PATH" in capsys.readouterr().out

    # 5. Simulated old Python version -> ERROR
    rep_old_py = run_doctor(cwd=tmp_path, python_version=(3, 10, 12))
    assert rep_old_py.has_errors is True
    assert "[ERROR] python_version" in format_doctor_text(rep_old_py)


# --- 4: Claude Code plugin packaging & bin launcher ---


def test_claude_plugin_manifests_and_bin_launcher() -> None:
    plugin_json = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    marketplace_json = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text("utf-8")
    )
    hooks_json = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text("utf-8"))

    assert plugin_json["name"] == "handoff-verify"
    assert plugin_json["version"] == __version__
    assert marketplace_json["name"] == "handoff-verify"
    assert marketplace_json["plugins"][0]["name"] == "handoff-verify"
    assert set(hooks_json["hooks"].keys()) == {"SubagentStop", "Stop"}
    for event_name in ("SubagentStop", "Stop"):
        cmd = hooks_json["hooks"][event_name][0]["hooks"][0]["command"]
        assert "${CLAUDE_PLUGIN_ROOT}/bin/handoff-verify" in cmd
        assert "hook claude-code" in cmd

    launcher = REPO_ROOT / "bin" / "handoff-verify"
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK)

    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    proc_ver = subprocess.run(
        [str(launcher), "--version"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc_ver.returncode == 0
    assert f"handoff-verify {__version__}" in proc_ver.stdout

    # Launcher fail-open on bad hook payload exits 1 (non-blocking, stderr visible)
    proc_hook = subprocess.run(
        [str(launcher), "hook", "claude-code"],
        input="{bad json",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc_hook.returncode == 1
    assert "handoff-verify hook:" in proc_hook.stderr

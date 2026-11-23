"""Regressions found by running handoff-verify on real Claude Code and Codex sessions.

Each test reproduces a pattern that either let a fabricated handoff through or flagged a
grounded one when the tool was run across ~1,600 real transcripts and inside a live
Claude Code session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import handoff_verify.hooks as hooks_mod
from handoff_verify.hooks import run_claude_code_hook
from handoff_verify.parsers import auto_parse
from handoff_verify.pipeline import verify_transcript


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


def _final(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _codes(tmp_path: Path, records: list[dict]) -> list[tuple[str, str]]:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    report = verify_transcript(auto_parse(path))
    return [(f.severity, f.code) for f in report.findings]


# --- Hook: the transcript file lags behind the payload's last_assistant_message ---


def _subagent_payload(transcript: Path, last_message: str, **extra: object) -> str:
    return json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "transcript_path": str(transcript.parent / "parent.jsonl"),
            "agent_transcript_path": str(transcript),
            "stop_hook_active": False,
            "last_assistant_message": last_message,
            **extra,
        }
    )


def test_hook_verifies_payload_message_when_transcript_has_no_assistant_text(
    tmp_path: Path,
) -> None:
    """Claude Code 2.1.x fired SubagentStop before the final message reached the JSONL file."""
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "isSidechain": True,
                "message": {"role": "user", "content": "Find the bug."},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    payload = _subagent_payload(
        transcript, "The bug is at `src/app.py:99` in `if req.user is None:`."
    )

    result = run_claude_code_hook(payload, mode="block", transcript_wait_seconds=0)

    assert result.exit_code == 2
    assert result.blocked is True
    assert "UNREAD_FILE" in result.stderr
    assert "src/app.py" in result.stderr


def test_hook_verifies_payload_message_instead_of_stale_previous_message(
    tmp_path: Path,
) -> None:
    """A lagging main transcript must not let the previous turn's text stand in for the final."""
    transcript = tmp_path / "session.jsonl"
    records = [
        _final("I'll look into it."),
        *_bash("t1", "ls src", "app.py\n"),
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "last_assistant_message": "The handler is at `src/missing.py:7`.",
        }
    )

    result = run_claude_code_hook(payload, mode="block", transcript_wait_seconds=0)

    assert result.exit_code == 2
    assert "src/missing.py" in result.stderr


def test_hook_does_not_duplicate_message_already_in_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    records = [
        *_bash("t1", "cat -n src/app.py", "     1\tdef main():\n     2\t    return 0\n"),
        _final("`src/app.py:1-2`   defines `main`."),
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "last_assistant_message": "`src/app.py:1-2` defines `main`.",
        }
    )

    result = run_claude_code_hook(payload, mode="block", transcript_wait_seconds=5)

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.report is not None
    assert len(result.report.claims) == 1


def test_hook_picks_up_message_flushed_while_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    reads = _bash("t1", "cat -n src/app.py", "     1\tdef main():\n     2\t    return 0\n")
    transcript.write_text("\n".join(json.dumps(r) for r in reads) + "\n", encoding="utf-8")
    final_text = "`src/app.py:1-2` defines `main`."

    def flush_then_sleep(_seconds: float) -> None:
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_final(final_text)) + "\n")

    monkeypatch.setattr(hooks_mod.time, "sleep", flush_then_sleep)
    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "last_assistant_message": final_text,
        }
    )

    result = run_claude_code_hook(payload, mode="block", transcript_wait_seconds=5)

    assert result.exit_code == 0
    assert result.report is not None
    assert result.stdout == ""
    assert len(result.report.claims) == 1


# --- Pointers: files read through shell commands the parser cannot window ---


def test_unified_diff_hunks_credit_new_side_line_numbers(tmp_path: Path) -> None:
    diff = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -40,4 +40,5 @@ def verify(token):\n"
        "     payload = decode(token)\n"
        "-    return payload\n"
        "+    if payload is None:\n"
        "+        raise AuthError()\n"
        "     audit(payload)\n"
        "     return payload\n"
    )
    records = [
        *_bash("t1", "git diff origin/main...HEAD", diff),
        _final("The new guard at `src/auth.py:41-42` raises before the audit call."),
    ]
    assert _codes(tmp_path, records) == []

    outside = [
        *_bash("t1", "git diff origin/main...HEAD", diff),
        _final("The retry loop at `src/auth.py:90` is unchanged."),
    ]
    assert ("error", "UNREAD_FILE") in _codes(tmp_path, outside)


def test_git_show_revision_path_pipeline_counts_as_a_read(tmp_path: Path) -> None:
    records = [
        *_bash(
            "t1",
            "cd /repo && git show origin/main:src/contexto/search.py | sed -n 70,95p",
            "def search(query):\n    return index.lookup(query)\n",
        ),
        _final("On main the lookup is `src/contexto/search.py:82`."),
    ]
    assert _codes(tmp_path, records) == []


def test_git_diff_stat_does_not_count_as_a_read(tmp_path: Path) -> None:
    records = [
        *_bash("t1", "git diff --stat HEAD~1 -- src/auth.py", " src/auth.py | 3 ++-\n"),
        _final("The guard is at `src/auth.py:41`."),
    ]
    assert ("error", "UNREAD_FILE") in _codes(tmp_path, records)


def test_awk_line_range_counts_as_a_read(tmp_path: Path) -> None:
    records = [
        *_bash(
            "t1",
            "awk 'NR>=195 && NR<=245 {printf \"%d\\t%s\\n\", NR, $0}' transfer/transfer.go",
            "214\tfunc parseAuthChallenge(h string) {\n",
        ),
        _final("Only the bounds check matches `transfer/transfer.go:214-230`."),
    ]
    assert _codes(tmp_path, records) == []


def test_range_with_both_endpoints_shown_by_grep_is_grounded(tmp_path: Path) -> None:
    grep_output = (
        "ui/auth/useAuth.ts:88:  const unAuthenticated = config.getAuthType() === undefined;\n"
        "ui/auth/useAuth.ts:95:  const [open, setOpen] = useState(unAuthenticated);\n"
    )
    records = [
        *_bash("t1", 'grep -rn "unAuthenticated" ui/', grep_output),
        _final("The dialog opens automatically (`ui/auth/useAuth.ts:88-95`)."),
    ]
    assert _codes(tmp_path, records) == []

    extrapolated = [
        *_bash("t1", 'grep -rn "unAuthenticated" ui/', grep_output),
        _final("The dialog opens automatically (`ui/auth/useAuth.ts:88-140`)."),
    ]
    assert ("error", "UNREAD_FILE") in _codes(tmp_path, extrapolated)


# --- Staleness: scratch copies and unexpanded shell variables are not the cited file ---


def test_sed_in_scratch_copy_does_not_stale_the_repo_file(tmp_path: Path) -> None:
    records = [
        *_bash("t1", "cat -n spikes/harness.py", "     1\tomitted = any(checks)\n"),
        *_bash(
            "t2",
            "rm -rf /tmp/patch6 && mkdir -p /tmp/patch6 && git archive HEAD spikes | "
            "tar -x -C /tmp/patch6 && cd /tmp/patch6 && "
            "sed -i '' 's/any(checks)/True/' spikes/harness.py",
            "",
        ),
        _final("The check is `spikes/harness.py:1`."),
    ]
    assert _codes(tmp_path, records) == []


def test_edit_through_unexpanded_shell_variable_is_not_attributed(tmp_path: Path) -> None:
    records = [
        *_bash("t1", "cat -n install.sh", "     1\tset -e\n     2\techo ok\n"),
        *_bash("t2", "B=/tmp/box; rm -rf $B/install.sh; cp install.sh $B/install.sh", ""),
        _final("`install.sh:2` prints ok."),
    ]
    assert _codes(tmp_path, records) == []


def test_in_place_edit_of_the_repo_file_still_goes_stale(tmp_path: Path) -> None:
    records = [
        *_bash("t1", "cat -n install.sh", "     1\tset -e\n     2\techo ok\n"),
        *_bash("t2", "sed -i '' '1d' install.sh", ""),
        _final("`install.sh:2` prints ok."),
    ]
    assert ("error", "STALE_FILE_READ") in _codes(tmp_path, records)


# --- Quotes: content printed by a pipeline has no file attribution ---


def test_quote_seen_in_unattributed_shell_output_is_not_misattributed(tmp_path: Path) -> None:
    records = [
        *_bash(
            "t1",
            "git show HEAD:scripts/ensure-gh.sh | sed -n 1,20p; echo ---; wc -l README.md",
            "version=$(gh --version) || true\n---\n      12 README.md\n",
        ),
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Write",
                        "input": {
                            "file_path": "/tmp/review/comment.md",
                            "content": "`version=$(gh --version) || true` swallows failures.\n",
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}],
            },
        },
        _final("In `scripts/ensure-gh.sh`: `version=$(gh --version) || true` swallows failures."),
    ]
    assert _codes(tmp_path, records) == []


def test_quote_that_only_appears_in_a_command_string_is_still_unfound(tmp_path: Path) -> None:
    records = [
        *_bash("t1", "cat -n src/app.py", "     1\tdef main():\n     2\t    return 0\n"),
        *_bash("t2", "grep -rn 'if origin not in ALLOWED_ORIGINS:' src/ ; echo done", "done\n"),
        _final("`src/app.py` checks `if origin not in ALLOWED_ORIGINS:` before responding."),
    ]
    assert ("error", "QUOTE_NOT_FOUND") in _codes(tmp_path, records)

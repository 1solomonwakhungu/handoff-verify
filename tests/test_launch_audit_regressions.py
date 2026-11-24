"""Regression tests for the public-launch audit fixes and cross-platform/edge-case coverage."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import handoff_verify.hooks as hooks_mod
from handoff_verify.cli import main
from handoff_verify.doctor import run_doctor
from handoff_verify.hooks import run_claude_code_hook
from handoff_verify.parsers import auto_parse
from handoff_verify.parsers._common import MAX_PARSE_WARNINGS
from handoff_verify.pipeline import verify_transcript
from handoff_verify.verifiers._paths import (
    build_base_dirs,
    is_abs_posix,
    is_exact_path_match,
    is_repo_root_path,
    normalize_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_windows_drive_paths_normalize_grep_and_scope(tmp_path: Path) -> None:
    base_dirs = build_base_dirs(r"C:\Users\alice\repo", None)
    assert is_abs_posix("C:/Users/alice/repo")
    assert is_abs_posix("c:/Users/alice/repo")
    assert not is_abs_posix("src/app.py")

    # Case-insensitive drive letter stripping
    assert normalize_path(r"C:\Users\alice\repo\src\auth.py", base_dirs=base_dirs) == "src/auth.py"
    assert normalize_path(r"c:\Users\alice\repo\src\auth.py", base_dirs=base_dirs) == "src/auth.py"
    assert normalize_path(r"C:\Users\alice\repo", base_dirs=base_dirs) == "."
    assert is_repo_root_path(r"C:\Users\alice\repo", base_dirs=base_dirs) is True
    assert is_repo_root_path(r"C:\Users\alice\repo\src\auth", base_dirs=base_dirs) is False
    assert is_exact_path_match("~/.claude/settings.json", r"C:\Users\alice\.claude\settings.json")

    # End-to-end Windows Claude Code transcript: Read + repo-wide Grep + Windows grep output
    records = [
        {
            "type": "assistant",
            "cwd": r"C:\Users\alice\repo",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "r1",
                        "name": "Read",
                        "input": {"file_path": r"C:\Users\alice\repo\src\auth.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "g1",
                        "name": "Grep",
                        "input": {
                            "pattern": "legacy_fn",
                            "path": r"C:\Users\alice\repo",
                            "output_mode": "content",
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "g2",
                        "name": "Grep",
                        "input": {
                            "pattern": "rate_limit",
                            "path": r"C:\Users\alice\repo",
                            "output_mode": "content",
                        },
                    },
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
                        "tool_use_id": "r1",
                        "content": "     1→def verify_token(tok):\n     2→    return bool(tok)\n",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "g1",
                        "content": "No matches found",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "g2",
                        "content": r"C:\Users\alice\repo\src\limits.py:14:MAX_RATE = 60",
                    },
                ],
            },
        },
        {
            "type": "assistant",
            "cwd": r"C:\Users\alice\repo",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Verified `src/auth.py:1-2` (`return bool(tok)`) and "
                            "`src/limits.py:14` (`MAX_RATE = 60`). "
                            "There are no callers of `legacy_fn` anywhere in the codebase."
                        ),
                    }
                ],
            },
        },
    ]
    report = verify_transcript(auto_parse(_write_jsonl(tmp_path / "win.jsonl", records)))
    assert report.passed is True
    assert report.findings == []

    # Subdirectory search on Windows MUST still trigger NARROW_SEARCH_SCOPE
    narrow_records = [
        {
            "type": "assistant",
            "cwd": r"C:\Users\alice\repo",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "g1",
                        "name": "Grep",
                        "input": {
                            "pattern": "legacy_fn",
                            "path": r"C:\Users\alice\repo\src\auth",
                            "output_mode": "content",
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "g1", "content": "No matches found"}
                ],
            },
        },
        {
            "type": "assistant",
            "cwd": r"C:\Users\alice\repo",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "There are no callers of `legacy_fn` anywhere in the codebase.",
                    }
                ],
            },
        },
    ]
    narrow_report = verify_transcript(
        auto_parse(_write_jsonl(tmp_path / "win_narrow.jsonl", narrow_records))
    )
    assert narrow_report.passed is False
    assert [f.code for f in narrow_report.findings] == ["NARROW_SEARCH_SCOPE"]


def test_unicode_paths_and_code_quotes_verify_cleanly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "café").mkdir(parents=True)
    target = repo / "src" / "café" / "résumé.py"
    target.write_text('def saluer(nom):\n    return f"Bonjour {nom} — 日本語"\n', encoding="utf-8")

    records = [
        {
            "type": "assistant",
            "cwd": str(repo),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "r1",
                        "name": "Read",
                        "input": {"file_path": str(target)},
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
                        "tool_use_id": "r1",
                        "content": (
                            '     1→def saluer(nom):\n     2→    return f"Bonjour {nom} — 日本語"\n'
                        ),
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "cwd": str(repo),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Checked `src/café/résumé.py:1-2` which returns "
                            '`return f"Bonjour {nom} — 日本語"`.'
                        ),
                    }
                ],
            },
        },
    ]
    report = verify_transcript(
        auto_parse(_write_jsonl(tmp_path / "unicode.jsonl", records)),
        repo_root=repo,
    )
    assert report.passed is True
    assert report.findings == []


def test_claude_bash_exit_code_zero_not_overridden_by_stdout_text(tmp_path: Path) -> None:
    records = [
        {
            "type": "assistant",
            "cwd": "/repo",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "b1",
                        "name": "Bash",
                        "input": {"command": "rg 'deprecated_api' ."},
                    }
                ],
            },
        },
        {
            "type": "user",
            "toolUseResult": {"exitCode": 0, "stdout": "Exit code: 1\n"},
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "b1",
                        "content": "Exit code: 1\n",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "cwd": "/repo",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
            },
        },
    ]
    parsed = auto_parse(_write_jsonl(tmp_path / "exit0.jsonl", records))
    assert len(parsed.commands) == 1
    assert parsed.commands[0].exit_code == 0


def test_claude_multiedit_with_invalid_entries_still_records_event(tmp_path: Path) -> None:
    records = [
        {
            "type": "assistant",
            "cwd": "/repo",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "m1",
                        "name": "MultiEdit",
                        "input": {"file_path": "src/a.py", "edits": ["not-a-dict", None]},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "m1", "content": "ok"}],
            },
        },
    ]
    parsed = auto_parse(_write_jsonl(tmp_path / "multiedit.jsonl", records))
    assert len(parsed.events) == 1
    assert len(parsed.file_edits) == 1
    assert parsed.file_edits[0].file_path == "src/a.py"


def test_orphan_tool_result_warnings_are_bounded_for_claude_and_codex(tmp_path: Path) -> None:
    claude_records = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"orphan_{i}", "content": "x"}
                    for i in range(120)
                ],
            },
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Ok."}]},
        },
    ]
    parsed_claude = auto_parse(_write_jsonl(tmp_path / "claude_orphans.jsonl", claude_records))
    assert len(parsed_claude.parse_warnings) == MAX_PARSE_WARNINGS + 1
    assert "additional orphan tool_result warnings" in parsed_claude.parse_warnings[-1]

    codex_records = [
        {"type": "session_meta", "payload": {"cwd": "/repo"}},
        *[
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": f"orphan_{i}",
                    "output": "ok",
                },
            }
            for i in range(120)
        ],
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Ok."}],
            },
        },
    ]
    parsed_codex = auto_parse(_write_jsonl(tmp_path / "codex_orphans.jsonl", codex_records))
    assert len(parsed_codex.parse_warnings) == MAX_PARSE_WARNINGS + 1
    assert "additional orphan tool output warnings" in parsed_codex.parse_warnings[-1]


def test_hook_wait_skips_reparse_when_transcript_file_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _write_jsonl(
        tmp_path / "session.jsonl",
        [
            {
                "type": "assistant",
                "cwd": str(tmp_path),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "r1",
                            "name": "Read",
                            "input": {"file_path": "src/a.py"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "r1", "content": "     1→x = 1\n"}
                    ],
                },
            },
        ],
    )

    real_auto_parse = hooks_mod.auto_parse
    parse_calls = 0

    def counting_auto_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return real_auto_parse(*args, **kwargs)

    monkeypatch.setattr(hooks_mod, "auto_parse", counting_auto_parse)
    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "last_assistant_message": "Checked `src/a.py:1`.",
        }
    )
    res = run_claude_code_hook(payload, mode="advise", transcript_wait_seconds=0.15)
    assert res.exit_code == 0
    assert parse_calls == 1


def test_live_repo_symlink_confinement_and_broken_symlinks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")

    real_file = repo / "src" / "real.py"
    real_file.write_text("def ok():\n    return 1\n", encoding="utf-8")

    inside_link = repo / "src" / "alias.py"
    inside_link.symlink_to(real_file)

    escape_link = repo / "src" / "escape.py"
    escape_link.symlink_to(outside)

    broken_local = repo / "src" / "broken.py"
    broken_local.symlink_to(repo / "src" / "missing_target.py")

    records = [
        {
            "type": "assistant",
            "cwd": str(repo),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "r1",
                        "name": "Read",
                        "input": {"file_path": "src/alias.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "r2",
                        "name": "Read",
                        "input": {"file_path": "src/escape.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "r3",
                        "name": "Read",
                        "input": {"file_path": "src/broken.py"},
                    },
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
                        "tool_use_id": "r1",
                        "content": "     1→def ok():\n     2→    return 1\n",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "r2",
                        "content": "     1→SECRET = 1\n",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "r3",
                        "content": "     1→x = 1\n",
                    },
                ],
            },
        },
        {
            "type": "assistant",
            "cwd": str(repo),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Checked `src/alias.py:1-2`, `src/escape.py:1`, and `src/broken.py:1`."
                        ),
                    }
                ],
            },
        },
    ]
    report = verify_transcript(
        auto_parse(_write_jsonl(tmp_path / "sym.jsonl", records)),
        repo_root=repo,
    )
    # Inside symlink passes, escaping symlink is safely skipped (never opened),
    # and broken in-repo symlink reports FILE_NOT_FOUND.
    assert [(f.claim.file_path, f.code) for f in report.findings if f.claim] == [
        ("src/broken.py", "FILE_NOT_FOUND")
    ]


def test_cli_eval_json_repo_tilde_init_env_and_doctor_v2_empty_plugin_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. `eval --json` shorthand works
    assert main(["eval", "--json"]) == 0
    eval_data = json.loads(capsys.readouterr().out)
    assert eval_data["passed_targets"] is True
    assert eval_data["metrics"]["total"] == 32

    # 2. `check --repo ~/myrepo` expands tilde cleanly
    fake_home = tmp_path / "home"
    myrepo = fake_home / "myrepo"
    (myrepo / "src").mkdir(parents=True)
    (myrepo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    tr = _write_jsonl(
        tmp_path / "tilde_repo.jsonl",
        [
            {"type": "meta", "cwd": str(myrepo)},
            {"type": "file_read", "file_path": "src/app.py", "start_line": 1, "content": "x = 1\n"},
            {"type": "assistant", "text": "Checked `src/app.py:1`."},
        ],
    )
    assert main(["check", str(tr), "--repo", "~/myrepo"]) == 0
    assert capsys.readouterr().out.startswith("PASS:")

    # 3. `init --write --command "FOO=1 <launcher>"` does not falsely warn on env prefix
    launcher = fake_home / "bin" / "hv"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    settings_path = tmp_path / ".claude" / "settings.json"
    assert (
        main(
            [
                "init",
                "--write",
                "--settings",
                str(settings_path),
                "--command",
                "FOO=1 ~/bin/hv",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Created Claude Code hooks" in captured.out
    assert captured.err == ""

    # 4. `doctor` ignores empty v2 plugin install list in installed_plugins.json
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    plugins_dir = fake_home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"handoff-verify@handoff-verify": []}}),
        encoding="utf-8",
    )
    doc = run_doctor(cwd=tmp_path / "fresh_cwd")
    hook_check = next(c for c in doc.checks if c.name == "claude_hooks")
    assert hook_check.details["plugin_installed"] is False
    assert hook_check.status == "warn"


def test_documented_finding_codes_match_verifier_source() -> None:
    """Prevent README.md and SKILL.md finding code tables from drifting from src/."""
    verifiers_dir = REPO_ROOT / "src" / "handoff_verify" / "verifiers"
    code_re = re.compile(r'\bcode="([A-Z][A-Z0-9_]+)"')
    actual_codes: set[str] = set()
    for py_file in verifiers_dir.glob("*.py"):
        actual_codes.update(code_re.findall(py_file.read_text(encoding="utf-8")))
    assert len(actual_codes) == 12

    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    skill_text = (REPO_ROOT / "skills" / "handoff-verify" / "SKILL.md").read_text(encoding="utf-8")

    # Every emitted code must be documented in both README.md and SKILL.md
    for code in sorted(actual_codes):
        assert f"`{code}`" in readme_text, f"{code} missing from README.md"
        assert f"`{code}`" in skill_text, f"{code} missing from SKILL.md"

    # Every code listed in SKILL.md's Finding Code table must exist in actual_codes
    table_section = skill_text.split("| Finding Code |", 1)[1].split("\n\n", 1)[0]
    skill_table_codes = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", table_section))
    assert skill_table_codes == actual_codes

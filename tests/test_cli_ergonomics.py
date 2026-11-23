"""CLI behaviors an agent or first-time user hits before reading the README."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from handoff_verify.cli import find_latest_claude_code_transcript, main

CASES = Path(__file__).resolve().parents[1] / "evals" / "cases"
CLEAN = str(CASES / "clean_01_claude_single_read.jsonl")
FAILING = str(CASES / "fail_01_claude_unread_pointer.jsonl")


def test_json_flag_is_shorthand_for_format_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", FAILING, "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert report["findings"][0]["code"] == "UNREAD_FILE"

    assert main(["inspect", CLEAN, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["source_format"] == "claude_code"


def test_md_is_accepted_for_markdown(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", FAILING, "--format", "md"]) == 1
    assert capsys.readouterr().out.startswith("### handoff-verify: FAIL")

    assert main(["check", FAILING, "--format", "yaml"]) == 2
    assert "choose from text, markdown, json" in capsys.readouterr().err


def test_missing_transcript_explains_where_transcripts_live(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check"]) == 2
    err = capsys.readouterr().err
    assert "--latest" in err
    assert "~/.claude/projects/" in err
    assert "~/.codex/sessions/" in err


def test_directory_argument_is_reported_as_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", str(tmp_path)]) == 2
    assert "is a directory" in capsys.readouterr().err


def test_non_jsonl_file_that_happens_to_parse_gets_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = tmp_path / "README.md"
    prose = "\n".join(f"Paragraph {i} of ordinary documentation." for i in range(40))
    example = json.dumps({"type": "assistant", "text": "All good."})
    readme.write_text(f"{prose}\n{example}\n", encoding="utf-8")

    assert main(["check", str(readme)]) == 0
    assert "may not be an agent transcript" in capsys.readouterr().err


def _session_dir(config_dir: Path, cwd: Path) -> Path:
    directory = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    directory.mkdir(parents=True)
    return directory


def test_latest_picks_newest_session_for_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "my.project"
    project.mkdir()
    config_dir = tmp_path / "claude-config"
    sessions = _session_dir(config_dir, project)
    older = sessions / "older.jsonl"
    newer = sessions / "newer.jsonl"
    older.write_text(Path(CLEAN).read_text(encoding="utf-8"), encoding="utf-8")
    newer.write_text(Path(FAILING).read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.chdir(project)

    found, _ = find_latest_claude_code_transcript(Path.cwd())
    assert found is not None and found.name == "newer.jsonl"

    assert main(["check", "--latest", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["transcript_path"].endswith("newer.jsonl")


def test_latest_without_sessions_lists_where_it_looked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    assert main(["check", "--latest"]) == 2
    err = capsys.readouterr().err
    assert "no Claude Code session transcript found" in err
    assert str(tmp_path / "claude-config" / "projects") in err

    assert main(["check", CLEAN, "--latest"]) == 2
    assert "not both" in capsys.readouterr().err


def test_init_write_warns_when_hook_command_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    assert main(["init", "--write"]) == 0
    captured = capsys.readouterr()
    assert "Created Claude Code hooks" in captured.out
    assert "`handoff-verify` is not on PATH" in captured.err

    launcher = tmp_path / "launcher"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    assert main(["init", "--write", "--command", str(launcher)]) == 0
    assert capsys.readouterr().err == ""


def test_help_shows_examples_and_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["--help"], ["check", "--help"]):
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert "examples:" in out
        assert "exit codes:" in out
        assert "--latest" in out

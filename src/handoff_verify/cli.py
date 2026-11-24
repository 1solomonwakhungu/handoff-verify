"""Command-line interface for handoff-verify."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from handoff_verify import __version__
from handoff_verify.doctor import (
    _extract_hook_executable,
    format_doctor_json,
    format_doctor_text,
    run_doctor,
)
from handoff_verify.evals import EvalSuiteError, format_eval_summary, run_eval_suite
from handoff_verify.formatters import format_report
from handoff_verify.hooks import (
    DEFAULT_CLAUDE_SETTINGS_PATH,
    SettingsError,
    build_claude_code_hook_command,
    build_claude_code_settings_snippet,
    run_claude_code_hook,
    write_claude_code_settings,
)
from handoff_verify.models import ParsedTranscript
from handoff_verify.parsers import auto_parse
from handoff_verify.pipeline import build_scope_receipt, verify_transcript


def _build_inspect_payload(transcript: ParsedTranscript) -> dict[str, Any]:
    scope = build_scope_receipt(transcript)
    final_preview = transcript.final_text
    if len(final_preview) > 300:
        final_preview = final_preview[:297] + "..."
    return {
        "transcript_path": transcript.transcript_path,
        "source_format": transcript.source_format,
        "cwd": transcript.cwd,
        "counts": {
            "events": len(transcript.events),
            "file_reads": len(transcript.file_reads),
            "file_edits": len(transcript.file_edits),
            "searches": len(transcript.searches),
            "commands": len(transcript.commands),
            "paginations": len(transcript.paginations),
            "assistant_messages": len(transcript.assistant_messages),
            "malformed_lines": transcript.malformed_lines,
        },
        "files_read": scope.files_read,
        "files_edited": scope.files_edited,
        "searched_roots": scope.searched_roots,
        "truncated_outputs": scope.truncated_outputs,
        "unread_cursors": scope.unread_cursors,
        "final_text_preview": final_preview,
        "parse_warnings": transcript.parse_warnings,
    }


def _format_inspect_text(transcript: ParsedTranscript) -> str:
    payload = _build_inspect_payload(transcript)
    counts = payload["counts"]
    lines = [
        f"Transcript: {payload['transcript_path'] or '<stdin>'}",
        f"Format:     {payload['source_format']}",
    ]
    if payload["cwd"]:
        lines.append(f"CWD:        {payload['cwd']}")
    lines.append(
        "Events:     "
        f"{counts['events']} total "
        f"({counts['file_reads']} reads, {counts['file_edits']} edits, "
        f"{counts['searches']} searches, {counts['commands']} commands, "
        f"{counts['paginations']} paginations)"
    )
    lines.append(f"Messages:   {counts['assistant_messages']} assistant message(s)")
    if payload["files_read"]:
        lines.append("Files read:")
        for fp in payload["files_read"]:
            lines.append(f"  - {fp}")
    if payload["files_edited"]:
        lines.append("Files edited:")
        for fp in payload["files_edited"]:
            lines.append(f"  - {fp}")
    if payload["truncated_outputs"]:
        lines.append(f"Truncated outputs: {len(payload['truncated_outputs'])}")
        for item in payload["truncated_outputs"]:
            lines.append(f"  - [{item['event_index']}] {item['tool_name']}: {item['reason']}")
    if payload["unread_cursors"]:
        lines.append(f"Unread pagination cursors: {len(payload['unread_cursors'])}")
        for item in payload["unread_cursors"]:
            lines.append(
                f"  - [{item['event_index']}] {item['tool_name']} ({item['resource']}): "
                f"next_cursor={item['next_cursor']}"
            )
    if payload["final_text_preview"]:
        one_line = " ".join(payload["final_text_preview"].split())
        lines.append(f"Final message: {one_line}")
    if counts["malformed_lines"] or payload["parse_warnings"]:
        lines.append(
            f"Warnings:   {len(payload['parse_warnings'])} "
            f"(malformed lines: {counts['malformed_lines']})"
        )
        for warning in payload["parse_warnings"]:
            lines.append(f"  - {warning}")
    return "\n".join(lines)


_EXIT_CODES_HELP = """\
exit codes:
  0  verification passed (or the command succeeded)
  1  findings: at least one claim is not backed by the transcript
  2  usage error, unreadable transcript, or internal error
"""

_TOP_EPILOG = (
    """\
examples:
  handoff-verify check --latest                 newest Claude Code session for this directory
  handoff-verify check session.jsonl --json     machine-readable report
  handoff-verify check - < session.jsonl        read the transcript from stdin
  handoff-verify inspect session.jsonl          what the agent read, edited and searched
  handoff-verify init --write                   install Claude Code Stop/SubagentStop hooks
  handoff-verify doctor                         check PATH, Python version, and hook config

transcripts:
  Claude Code   ~/.claude/projects/<cwd-with-dashes>/<session-id>.jsonl
                subagents: .../<session-id>/subagents/agent-<id>.jsonl
  Codex CLI     ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

"""
    + _EXIT_CODES_HELP
)

_CHECK_EPILOG = (
    """\
examples:
  handoff-verify check --latest
  handoff-verify check session.jsonl --json | jq '.findings[] | {code, message, suggestion}'
  handoff-verify check session.jsonl --repo . --format markdown
  cat session.jsonl | handoff-verify check - --quiet-on-pass

Each finding carries a stable `code`, the offending `claim`, the `evidence` checked and a
`suggestion` naming the read or search that would ground the claim.

"""
    + _EXIT_CODES_HELP
)

_HOOK_EPILOG = """\
Reads the Claude Code hook JSON payload on stdin (hook_event_name, transcript_path,
agent_transcript_path, last_assistant_message, stop_hook_active, cwd).

  advise  exit 0; on errors prints {"systemMessage", "hookSpecificOutput.additionalContext"}
          so the model sees the findings once and can correct its report
  block   exit 2 with the findings on stderr, so the agent must fix them before stopping

Silent on clean and warning-only reports by default (pass --include-warnings to surface
warnings as systemMessage, or --strict to treat warnings as errors).
Never blocks twice in a row: when stop_hook_active is true it only prints a systemMessage.
Fails open (exit 1, non-blocking in Claude Code while surfacing stderr) on unreadable input
or internal errors unless --fail-closed (exit 2) is given.

example:
  handoff-verify hook claude-code --mode block < payload.json
"""

_DOCTOR_EPILOG = """\
examples:
  handoff-verify doctor
  handoff-verify doctor --json
  handoff-verify doctor --settings ~/.claude/settings.json

exit codes:
  0  no error-level issues (ready, or setup warnings with next steps printed)
  1  error-level issue found (e.g. malformed settings.json, missing hook binary, Python < 3.11)
"""

_INIT_EPILOG = """\
examples:
  handoff-verify init                          print the hook JSON without changing anything
  handoff-verify init --write                  merge it into ./.claude/settings.json
  handoff-verify init --write --mode block     make the agent fix findings before stopping
  handoff-verify init --write --settings ~/.claude/settings.json      every project
  handoff-verify init --write --command "uvx handoff-verify"          no global install

The hook command must resolve on the PATH of the non-interactive shell Claude Code uses.
"""

_FORMAT_ALIASES = {"md": "markdown"}


def _format_choice(choices: tuple[str, ...]) -> Callable[[str], str]:
    """Build an argparse `type` accepting the listed formats plus common aliases."""

    def parse(value: str) -> str:
        normalized = _FORMAT_ALIASES.get(value.lower(), value.lower())
        if normalized not in choices:
            raise argparse.ArgumentTypeError(
                f"invalid choice: {value!r} (choose from {', '.join(choices)})"
            )
        return normalized

    return parse


def claude_code_project_dirs(cwd: Path) -> list[Path]:
    """Return candidate Claude Code transcript directories for a working directory."""
    roots: list[Path] = []
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        roots.append(Path(config_dir).expanduser())
    roots.append(Path.home() / ".claude")

    spellings = [str(cwd)]
    with contextlib.suppress(OSError):
        spellings.append(str(cwd.resolve()))

    candidates: list[Path] = []
    for root in roots:
        for spelling in spellings:
            candidate = root / "projects" / re.sub(r"[^A-Za-z0-9]", "-", spelling)
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def find_latest_claude_code_transcript(cwd: Path) -> tuple[Path | None, list[Path]]:
    """Return the most recently modified main-session transcript for `cwd`."""
    searched = claude_code_project_dirs(cwd)
    newest: Path | None = None
    newest_mtime = -1.0
    for directory in searched:
        if not directory.is_dir():
            continue
        for path in directory.glob("*.jsonl"):
            with contextlib.suppress(OSError):
                mtime = path.stat().st_mtime
                if mtime > newest_mtime:
                    newest, newest_mtime = path, mtime
    return newest, searched


def _resolve_transcript_arg(args: argparse.Namespace) -> str | None:
    """Return the transcript argument, resolving --latest; print a usage hint on failure."""
    raw_arg: str | None = args.transcript
    if getattr(args, "latest", False):
        if raw_arg is not None:
            print("Error: pass either a transcript path or --latest, not both.", file=sys.stderr)
            return None
        latest, searched = find_latest_claude_code_transcript(Path.cwd())
        if latest is None:
            listing = "\n".join(f"  {d}" for d in searched)
            print(
                "Error: no Claude Code session transcript found for this directory. Looked in:\n"
                f"{listing}\n"
                "Pass a transcript path explicitly, or run from the project directory.",
                file=sys.stderr,
            )
            return None
        return str(latest)
    if raw_arg is None:
        print(
            "Error: missing transcript. Pass a JSONL transcript path, '-' to read stdin, or "
            "--latest for the newest Claude Code session in this directory.\n"
            "  Claude Code: ~/.claude/projects/<cwd-with-dashes>/<session-id>.jsonl\n"
            "  Codex CLI:   ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl",
            file=sys.stderr,
        )
    return raw_arg


def _load_transcript_arg(raw_arg: str, parser_hint: str) -> ParsedTranscript | None:
    if raw_arg == "-":
        return auto_parse(sys.stdin, format_hint=parser_hint, transcript_path="<stdin>")

    transcript_path = Path(raw_arg).expanduser()
    if transcript_path.is_dir():
        print(
            f"Error: {transcript_path} is a directory, expected a JSONL transcript file. "
            "Use --latest to pick the newest Claude Code session for the current directory.",
            file=sys.stderr,
        )
        return None
    if not transcript_path.is_file():
        print(f"Error: transcript file not found: {transcript_path}", file=sys.stderr)
        return None
    return auto_parse(transcript_path, format_hint=parser_hint)


def _warn_if_mostly_not_jsonl(transcript: ParsedTranscript) -> None:
    """Warn on stderr when most lines failed to parse (a Markdown file, a log, a diff)."""
    recognized = len(transcript.events) + len(transcript.assistant_messages)
    if transcript.malformed_lines > max(recognized, 3):
        print(
            f"Warning: {transcript.malformed_lines} line(s) were not valid JSON and only "
            f"{recognized} transcript record(s) were recognized; this may not be an agent "
            "transcript.",
            file=sys.stderr,
        )


def _cmd_inspect(args: argparse.Namespace) -> int:
    raw_arg = _resolve_transcript_arg(args)
    if raw_arg is None:
        return 2
    try:
        transcript = _load_transcript_arg(raw_arg, args.parser)
    except (OSError, ValueError) as exc:
        print(f"Error: failed to read transcript: {exc}", file=sys.stderr)
        return 2
    if transcript is None:
        return 2

    _warn_if_mostly_not_jsonl(transcript)
    if args.json or args.format == "json":
        print(json.dumps(_build_inspect_payload(transcript), indent=2))
    else:
        print(_format_inspect_text(transcript))
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    repo_root: Path | None = None
    if args.repo is not None:
        repo_root = Path(args.repo).expanduser()
        if not repo_root.is_dir():
            print(f"Error: repository directory not found: {repo_root}", file=sys.stderr)
            return 2

    raw_arg = _resolve_transcript_arg(args)
    if raw_arg is None:
        return 2
    try:
        transcript = _load_transcript_arg(raw_arg, args.parser)
    except (OSError, ValueError) as exc:
        print(f"Error: failed to read transcript: {exc}", file=sys.stderr)
        return 2
    if transcript is None:
        return 2

    # Never report PASS when zero events and zero assistant messages were recognized
    # (catches empty files, READMEs, garbage, summary-only files, and forced --parser flags).
    if not transcript.events and not transcript.assistant_messages:
        source_label = transcript.transcript_path or raw_arg
        print(
            f"Error: no recognizable transcript events or assistant messages found in "
            f"{source_label} (format={transcript.source_format}, "
            f"malformed lines={transcript.malformed_lines}). Expected a Claude Code, Codex "
            f"CLI, or generic JSONL transcript; `handoff-verify --help` lists where they live.",
            file=sys.stderr,
        )
        return 2

    _warn_if_mostly_not_jsonl(transcript)
    report = verify_transcript(transcript, repo_root=repo_root, strict=args.strict)

    output_format = "json" if args.json else args.format
    rendered = format_report(report, output_format, quiet_on_pass=args.quiet_on_pass)
    if rendered:
        print(rendered)

    return 0 if report.passed else 1


def _cmd_hook_claude_code(args: argparse.Namespace) -> int:
    fail_closed = bool(args.fail_closed)
    try:
        stdin_text = sys.stdin.read()
        repo_root = Path(args.repo).expanduser() if args.repo is not None else None
        result = run_claude_code_hook(
            stdin_text,
            mode=args.mode,
            strict=bool(args.strict),
            include_warnings=bool(args.include_warnings),
            repo_root=repo_root,
            fail_closed=fail_closed,
        )
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return result.exit_code
    except Exception as exc:
        with contextlib.suppress(Exception):
            print(f"handoff-verify hook: internal error: {exc}", file=sys.stderr)
        return 2 if fail_closed else 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    settings_path = Path(args.settings) if args.settings is not None else None
    report = run_doctor(settings_path=settings_path)
    if args.json or args.format == "json":
        print(format_doctor_json(report))
    else:
        print(format_doctor_text(report))
    return 1 if report.has_errors else 0


def _cmd_init(args: argparse.Namespace) -> int:
    if not args.write:
        snippet = build_claude_code_settings_snippet(
            mode=args.mode,
            strict=bool(args.strict),
            command=args.command_override,
        )
        print(json.dumps(snippet, indent=2, ensure_ascii=False))
        return 0

    settings_path = Path(args.settings).expanduser()
    hook_cmd = build_claude_code_hook_command(
        mode=args.mode,
        strict=bool(args.strict),
        command=args.command_override,
    )
    try:
        _, created = write_claude_code_settings(
            settings_path,
            mode=args.mode,
            strict=bool(args.strict),
            command=args.command_override,
        )
    except (SettingsError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    action = "Created" if created else "Updated"
    print(
        f"{action} Claude Code hooks in {settings_path} (SubagentStop, Stop; command={hook_cmd!r})."
    )
    executable = _hook_executable(hook_cmd)
    if executable and not _hook_executable_resolves(executable):
        print(
            f"Warning: `{executable}` is not on PATH, so the hook will fail with 'command not "
            "found' on every stop. Install the CLI (`uv tool install handoff-verify`) or "
            're-run with --command "<absolute path or launcher>".',
            file=sys.stderr,
        )
    return 0


def _hook_executable(hook_command: str) -> str | None:
    """Return the executable invoking handoff-verify, skipping env vars and shell ops."""
    return _extract_hook_executable(hook_command)


def _hook_executable_resolves(executable: str) -> bool:
    """Return True if `executable` resolves on PATH or points to an existing local file."""
    if "${" in executable or "$" in executable:
        return True
    exe_path = Path(executable).expanduser()
    if exe_path.is_absolute() or "/" in executable or "\\" in executable:
        return exe_path.is_file()
    return shutil.which(executable) is not None


def _cmd_eval(args: argparse.Namespace) -> int:
    cases_dir = Path(args.cases_dir) if args.cases_dir is not None else None
    try:
        summary = run_eval_suite(cases_dir=cases_dir, strict=bool(args.strict))
    except (EvalSuiteError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    output_format = "json" if args.json else args.format
    print(format_eval_summary(summary, output_format, verbose=bool(args.verbose)))
    return 0 if summary.passed_targets else 1


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser for handoff-verify."""
    parser = argparse.ArgumentParser(
        prog="handoff-verify",
        description=(
            "Deterministic, zero-LLM verification of AI coding agent handoff claims "
            "against tool-call transcripts."
        ),
        epilog=_TOP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Parse and summarize a transcript's format, tool events, and warnings.",
        description="Show what a transcript's agent actually read, edited, searched and said.",
    )
    inspect_parser.add_argument(
        "transcript",
        nargs="?",
        default=None,
        help="Path to the JSONL transcript file (or '-' for stdin).",
    )
    inspect_parser.add_argument(
        "--latest",
        action="store_true",
        help="Use the newest Claude Code session transcript for the current directory.",
    )
    inspect_parser.add_argument(
        "--format",
        type=_format_choice(("text", "json")),
        metavar="{text,json}",
        default="text",
        help="Output format (default: text).",
    )
    inspect_parser.add_argument(
        "--json",
        action="store_true",
        help="Shorthand for --format json.",
    )
    inspect_parser.add_argument(
        "--parser",
        choices=("auto", "claude_code", "codex", "generic"),
        default="auto",
        help="Force a specific transcript parser instead of auto-detection.",
    )
    inspect_parser.set_defaults(func=_cmd_inspect)

    check_parser = subparsers.add_parser(
        "check",
        help="Verify claims in a transcript against observed tool-call evidence.",
        description=(
            "Check every file pointer, verbatim quote and absence/completeness claim in the "
            "agent's final message against what its tool calls actually observed."
        ),
        epilog=_CHECK_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check_parser.add_argument(
        "transcript",
        nargs="?",
        default=None,
        help="Path to the JSONL transcript file (or '-' for stdin).",
    )
    check_parser.add_argument(
        "--latest",
        action="store_true",
        help="Use the newest Claude Code session transcript for the current directory.",
    )
    check_parser.add_argument(
        "--repo",
        default=None,
        help="Optional repository root for live file/git state verification.",
    )
    check_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as verification failures.",
    )
    check_parser.add_argument(
        "--format",
        type=_format_choice(("text", "markdown", "json")),
        metavar="{text,markdown,json}",
        default="text",
        help="Output format (default: text). `md` is accepted for markdown.",
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="Shorthand for --format json.",
    )
    check_parser.add_argument(
        "--quiet-on-pass",
        action="store_true",
        help="Suppress output when verification passes with zero findings.",
    )
    check_parser.add_argument(
        "--parser",
        choices=("auto", "claude_code", "codex", "generic"),
        default="auto",
        help="Force a specific transcript parser instead of auto-detection.",
    )
    check_parser.set_defaults(func=_cmd_check)

    hook_parser = subparsers.add_parser(
        "hook",
        help="Run a deterministic verification hook adapter for an AI coding harness.",
    )
    hook_subparsers = hook_parser.add_subparsers(dest="hook_target", required=True)

    claude_hook_parser = hook_subparsers.add_parser(
        "claude-code",
        help="Handle Claude Code Stop / SubagentStop hook JSON payloads on stdin.",
        allow_abbrev=False,
        epilog=_HOOK_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    claude_hook_parser.add_argument(
        "--mode",
        choices=("advise", "block"),
        default="advise",
        help="advise: exit 0 with feedback JSON (default); block: exit 2 with stderr receipt.",
    )
    claude_hook_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as verification failures.",
    )
    claude_hook_parser.add_argument(
        "--include-warnings",
        action="store_true",
        help="Emit systemMessage feedback on warning-only reports (default: silent on warnings).",
    )
    claude_hook_parser.add_argument(
        "--repo",
        default=None,
        help="Optional repository root (defaults to hook payload cwd if valid).",
    )
    claude_hook_parser.add_argument(
        "--fail-closed",
        action="store_true",
        help="Exit 2 (blocking) on internal/parse/IO errors instead of failing open (exit 1).",
    )
    claude_hook_parser.set_defaults(func=_cmd_hook_claude_code)

    init_parser = subparsers.add_parser(
        "init",
        help="Print or install harness hook configuration (e.g., Claude Code settings.json).",
        epilog=_INIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    init_parser.add_argument(
        "--claude-code",
        action="store_true",
        help="Target Claude Code Stop / SubagentStop hooks (the default and only harness).",
    )
    init_parser.add_argument(
        "--write",
        action="store_true",
        help="Safely create or merge hooks into the Claude settings.json file.",
    )
    init_parser.add_argument(
        "--settings",
        default=str(DEFAULT_CLAUDE_SETTINGS_PATH),
        help="Target settings.json path for --write (default: .claude/settings.json).",
    )
    init_parser.add_argument(
        "--mode",
        choices=("advise", "block"),
        default="advise",
        help="Hook mode to configure (default: advise).",
    )
    init_parser.add_argument(
        "--strict",
        action="store_true",
        help="Include --strict in the configured hook command.",
    )
    init_parser.add_argument(
        "--command",
        dest="command_override",
        default=None,
        help=(
            "Shell command fragment prefix (e.g. 'uvx handoff-verify') or full hook "
            "command override containing 'hook claude-code' (default: handoff-verify)."
        ),
    )
    init_parser.set_defaults(func=_cmd_init)

    eval_parser = subparsers.add_parser(
        "eval",
        help="Run the seeded evaluation suite and report precision, recall, and latency.",
    )
    eval_parser.add_argument(
        "--cases-dir",
        default=None,
        help="Path to the evals directory containing manifest.json (auto-discovered if omitted).",
    )
    eval_parser.add_argument(
        "--format",
        type=_format_choice(("text", "markdown", "json")),
        metavar="{text,markdown,json}",
        default="text",
        help="Output format (default: text).",
    )
    eval_parser.add_argument(
        "--json",
        action="store_true",
        help="Shorthand for --format json.",
    )
    eval_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as verification failures during eval.",
    )
    eval_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show per-case outcomes and timings for every eval case.",
    )
    eval_parser.set_defaults(func=_cmd_eval)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check binary on PATH, Python version, and Claude Code hook configuration.",
        description=(
            "Diagnose your local handoff-verify installation and Claude Code hook setup, "
            "printing actionable next steps."
        ),
        epilog=_DOCTOR_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    doctor_parser.add_argument(
        "--settings",
        default=None,
        help="Optional specific Claude settings.json path to inspect.",
    )
    doctor_parser.add_argument(
        "--format",
        type=_format_choice(("text", "json")),
        metavar="{text,json}",
        default="text",
        help="Output format (default: text).",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Shorthand for --format json.",
    )
    doctor_parser.set_defaults(func=_cmd_doctor)

    return parser


def _hook_argv_flags(argv: list[str] | None) -> tuple[bool, bool]:
    """Return ``(is_hook_subcommand, fail_closed_requested)`` from raw CLI argv."""
    tokens = sys.argv[1:] if argv is None else list(argv)
    is_hook = bool(tokens) and tokens[0] == "hook"
    fail_closed = "--fail-closed" in tokens
    return is_hook, fail_closed


def main(argv: list[str] | None = None) -> int:
    """CLI entry point returning POSIX exit code (0=pass, 1=findings, 2=usage/internal error)."""
    is_hook, fail_closed = _hook_argv_flags(argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        if is_hook and code != 0 and not fail_closed:
            return 1
        return code
    try:
        return int(args.func(args))
    except Exception as exc:
        with contextlib.suppress(Exception):
            print(f"Error: internal failure: {exc}", file=sys.stderr)
        if is_hook and not fail_closed:
            return 1
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-line interface for handoff-verify."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from handoff_verify import __version__
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


def _load_transcript_arg(raw_arg: str, parser_hint: str) -> ParsedTranscript | None:
    if raw_arg == "-":
        return auto_parse(sys.stdin, format_hint=parser_hint, transcript_path="<stdin>")

    transcript_path = Path(raw_arg)
    if not transcript_path.is_file():
        print(f"Error: transcript file not found: {transcript_path}", file=sys.stderr)
        return None
    return auto_parse(transcript_path, format_hint=parser_hint)


def _cmd_inspect(args: argparse.Namespace) -> int:
    try:
        transcript = _load_transcript_arg(args.transcript, args.parser)
    except (OSError, ValueError) as exc:
        print(f"Error: failed to read transcript: {exc}", file=sys.stderr)
        return 2
    if transcript is None:
        return 2

    if args.format == "json":
        print(json.dumps(_build_inspect_payload(transcript), indent=2))
    else:
        print(_format_inspect_text(transcript))
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    repo_root: Path | None = None
    if args.repo is not None:
        repo_root = Path(args.repo)
        if not repo_root.is_dir():
            print(f"Error: repository directory not found: {repo_root}", file=sys.stderr)
            return 2

    try:
        transcript = _load_transcript_arg(args.transcript, args.parser)
    except (OSError, ValueError) as exc:
        print(f"Error: failed to read transcript: {exc}", file=sys.stderr)
        return 2
    if transcript is None:
        return 2

    # Never report PASS when zero events and zero assistant messages were recognized
    # (catches empty files, READMEs, garbage, summary-only files, and forced --parser flags).
    if not transcript.events and not transcript.assistant_messages:
        source_label = transcript.transcript_path or args.transcript
        print(
            f"Error: no recognizable transcript events or assistant messages found in "
            f"{source_label} (format={transcript.source_format}, "
            f"malformed lines={transcript.malformed_lines})",
            file=sys.stderr,
        )
        return 2

    report = verify_transcript(transcript, repo_root=repo_root, strict=args.strict)

    rendered = format_report(report, args.format, quiet_on_pass=args.quiet_on_pass)
    if rendered:
        print(rendered)

    return 0 if report.passed else 1


def _cmd_hook_claude_code(args: argparse.Namespace) -> int:
    fail_closed = bool(args.fail_closed)
    try:
        stdin_text = sys.stdin.read()
        repo_root = Path(args.repo) if args.repo is not None else None
        result = run_claude_code_hook(
            stdin_text,
            mode=args.mode,
            strict=bool(args.strict),
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
        return 2 if fail_closed else 0


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
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    cases_dir = Path(args.cases_dir) if args.cases_dir is not None else None
    try:
        summary = run_eval_suite(cases_dir=cases_dir, strict=bool(args.strict))
    except (EvalSuiteError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(format_eval_summary(summary, args.format, verbose=bool(args.verbose)))
    return 0 if summary.passed_targets else 1


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser for handoff-verify."""
    parser = argparse.ArgumentParser(
        prog="handoff-verify",
        description=(
            "Deterministic, zero-LLM verification of AI coding agent handoff claims "
            "against tool-call transcripts."
        ),
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
    )
    inspect_parser.add_argument(
        "transcript",
        help="Path to the JSONL transcript file (or '-' for stdin).",
    )
    inspect_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
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
    )
    check_parser.add_argument(
        "transcript",
        help="Path to the JSONL transcript file (or '-' for stdin).",
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
        choices=("text", "markdown", "json"),
        default="text",
        help="Output format (default: text).",
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
        "--repo",
        default=None,
        help="Optional repository root (defaults to hook payload cwd if valid).",
    )
    claude_hook_parser.add_argument(
        "--fail-closed",
        action="store_true",
        help="Exit 2 on internal/parse/IO errors instead of failing open (exit 0).",
    )
    claude_hook_parser.set_defaults(func=_cmd_hook_claude_code)

    init_parser = subparsers.add_parser(
        "init",
        help="Print or install harness hook configuration (e.g., Claude Code settings.json).",
    )
    init_parser.add_argument(
        "--claude-code",
        action="store_true",
        required=True,
        help="Generate or install Claude Code Stop / SubagentStop hooks.",
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
        choices=("text", "markdown", "json"),
        default="text",
        help="Output format (default: text).",
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
            return 0
        return code
    try:
        return int(args.func(args))
    except Exception as exc:
        with contextlib.suppress(Exception):
            print(f"Error: internal failure: {exc}", file=sys.stderr)
        if is_hook and not fail_closed:
            return 0
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

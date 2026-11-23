"""Command-line interface for handoff-verify."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from handoff_verify import __version__
from handoff_verify.models import ParsedTranscript, VerificationReport
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


def _format_check_output(report: VerificationReport, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report.to_dict(), indent=2)

    status = "PASS" if report.passed else "FAIL"
    summary = (
        f"{len(report.findings)} findings "
        f"({len(report.claims)} claims checked, "
        f"{len(report.scope_receipt.files_read)} files read, "
        f"{report.duration_ms:.1f}ms)"
    )

    if fmt == "markdown":
        if report.passed and not report.findings:
            return f"**handoff-verify:** {status} — {summary}"
        lines = [f"### handoff-verify: {status}", "", f"Summary: {summary}", ""]
        for finding in report.findings:
            lines.append(f"- **[{finding.severity.upper()}] `{finding.code}`**: {finding.message}")
            if finding.suggestion:
                lines.append(f"  - Suggestion: {finding.suggestion}")
        return "\n".join(lines)

    # Plain text
    if report.passed and not report.findings:
        return f"{status}: {summary}"
    lines = [f"{status}: {summary}"]
    for finding in report.findings:
        lines.append(f"  [{finding.severity.upper()}] {finding.code}: {finding.message}")
        if finding.suggestion:
            lines.append(f"    -> {finding.suggestion}")
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

    # Never report PASS on empty, unreadable, or completely unrecognized input files
    if (
        transcript.source_format == "unknown"
        and not transcript.events
        and not transcript.assistant_messages
    ):
        source_label = transcript.transcript_path or args.transcript
        print(
            f"Error: no recognizable transcript records found in {source_label} "
            f"(malformed lines: {transcript.malformed_lines})",
            file=sys.stderr,
        )
        return 2

    report = verify_transcript(transcript, repo_root=repo_root, strict=args.strict)

    if not (report.passed and args.quiet_on_pass):
        print(_format_check_output(report, args.format))

    return 0 if report.passed else 1


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
        help="Suppress output when verification passes.",
    )
    check_parser.add_argument(
        "--parser",
        choices=("auto", "claude_code", "codex", "generic"),
        default="auto",
        help="Force a specific transcript parser instead of auto-detection.",
    )
    check_parser.set_defaults(func=_cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point returning POSIX exit code (0=pass, 1=findings, 2=usage/internal error)."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 2
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"Error: internal failure: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

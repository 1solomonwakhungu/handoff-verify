"""Hook adapters and setup helpers for AI coding harnesses (Claude Code)."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import shlex
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from handoff_verify.formatters import format_hook_feedback
from handoff_verify.models import VerificationReport
from handoff_verify.parsers import auto_parse
from handoff_verify.pipeline import verify_transcript

HookMode = Literal["advise", "block"]

CLAUDE_CODE_EVENTS: tuple[str, ...] = ("SubagentStop", "Stop")
DEFAULT_CLAUDE_SETTINGS_PATH = Path(".claude/settings.json")
_SHELL_COMPOUND_OPERATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})


class SettingsError(ValueError):
    """Raised when an existing Claude Code settings.json cannot be safely merged."""


@dataclass
class ClaudeHookInput:
    """Normalized fields parsed from a Claude Code Stop/SubagentStop hook payload."""

    hook_event_name: str | None = None
    transcript_path: str | None = None
    agent_transcript_path: str | None = None
    cwd: str | None = None
    stop_hook_active: bool = False
    session_id: str | None = None
    agent_id: str | None = None
    last_assistant_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    """Outcome of executing a hook adapter."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    blocked: bool = False
    report: VerificationReport | None = None


def _non_empty_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def _parse_stop_hook_active(value: Any) -> bool:
    """Parse ``stop_hook_active`` conservatively (unknown non-empty strings count as active)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized not in ("", "false", "0", "no", "off")
    return bool(value)


def parse_claude_hook_input(stdin_text: str) -> ClaudeHookInput:
    """Parse and validate a Claude Code hook JSON payload from stdin text."""
    stripped = stdin_text.strip()
    if not stripped:
        raise ValueError("empty hook payload on stdin")

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON payload: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object payload, got {type(payload).__name__}")

    return ClaudeHookInput(
        hook_event_name=_non_empty_str(payload.get("hook_event_name")),
        transcript_path=_non_empty_str(payload.get("transcript_path")),
        agent_transcript_path=_non_empty_str(payload.get("agent_transcript_path")),
        cwd=_non_empty_str(payload.get("cwd")),
        stop_hook_active=_parse_stop_hook_active(payload.get("stop_hook_active", False)),
        session_id=_non_empty_str(payload.get("session_id")),
        agent_id=_non_empty_str(payload.get("agent_id")),
        last_assistant_message=_non_empty_str(payload.get("last_assistant_message")),
        raw=payload,
    )


def _select_transcript_path(
    hook_input: ClaudeHookInput,
    cwd_dir: Path | None,
) -> tuple[Path | None, str | None]:
    """Select the appropriate transcript path for Stop vs SubagentStop events.

    Returns ``(resolved_path, missing_reason)``.

    On ``SubagentStop``, ``transcript_path`` belongs to the parent session, so we
    MUST use ``agent_transcript_path`` and never fall back to the parent session's
    transcript (which would verify or block a subagent for parent claims).
    On ``Stop``, we use ``transcript_path``.
    On unknown/omitted event names, we prefer ``agent_transcript_path`` or
    fall back to ``transcript_path``.
    """
    if hook_input.hook_event_name == "SubagentStop":
        raw_path = hook_input.agent_transcript_path
        if raw_path is None:
            return None, "missing agent_transcript_path in SubagentStop hook payload"
    elif hook_input.hook_event_name == "Stop":
        raw_path = hook_input.transcript_path
        if raw_path is None:
            return None, "missing transcript_path in Stop hook payload"
    else:
        raw_path = hook_input.agent_transcript_path or hook_input.transcript_path
        if raw_path is None:
            return None, "missing transcript_path in hook payload"

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute() and not candidate.is_file() and cwd_dir is not None:
        cwd_candidate = cwd_dir / candidate
        if cwd_candidate.is_file():
            return cwd_candidate, None
    return candidate, None


def _build_hook_json(
    feedback: str,
    hook_event_name: str | None,
    *,
    include_additional_context: bool,
) -> str:
    """Build Claude Code hook JSON for stdout.

    Note: On Claude Code ``Stop`` and ``SubagentStop`` hooks, emitting
    ``hookSpecificOutput.additionalContext`` continues the turn so the model can
    act on the feedback (subject to loop protection), whereas ``systemMessage``
    alone notifies the user without continuing the turn. Requires a Claude Code
    version supporting ``hookSpecificOutput`` on Stop/SubagentStop when
    ``include_additional_context=True``.
    """
    payload: dict[str, Any] = {"systemMessage": feedback}
    if include_additional_context and hook_event_name:
        payload["hookSpecificOutput"] = {
            "hookEventName": hook_event_name,
            "additionalContext": feedback,
        }
    return json.dumps(payload)


def _fail_result(message: str, *, fail_closed: bool) -> HookResult:
    """Return a fail-open (exit 0) or fail-closed (exit 2) diagnostic result."""
    clean_msg = " ".join(message.split())
    return HookResult(
        exit_code=2 if fail_closed else 0,
        stdout="",
        stderr=f"handoff-verify hook: {clean_msg}",
        blocked=False,
        report=None,
    )


def run_claude_code_hook(
    stdin_text: str,
    *,
    mode: HookMode = "advise",
    strict: bool = False,
    repo_root: Path | None = None,
    fail_closed: bool = False,
) -> HookResult:
    """Execute the Claude Code Stop / SubagentStop verification hook.

    Behavior:
    - Clean verification (``report.passed`` with zero findings): exits ``0`` silently.
    - Loop guard (``stop_hook_active=True``): NEVER exits ``2`` and NEVER emits
      ``hookSpecificOutput.additionalContext`` (which continues the turn in Claude
      Code). Emits ``{"systemMessage": ...}`` only and exits ``0`` in BOTH
      ``advise`` and ``block`` modes so the agent can always stop cleanly.
    - ``mode="advise"`` (default, ``stop_hook_active=False``):
      - On verification failure (``not report.passed``): exits ``0`` with
        ``systemMessage`` (and ``hookSpecificOutput.additionalContext`` when
        ``hook_event_name`` is known for a single bounded soft continuation).
      - On non-failing warnings/notes (``report.passed=True`` with findings):
        exits ``0`` with ``systemMessage`` only (no turn continuation).
    - ``mode="block"`` (``stop_hook_active=False``):
      - On verification failure (``not report.passed``): writes concise actionable
        receipt to stderr and exits ``2`` to block stopping (max 1 block via
        ``stop_hook_active``).
      - On non-failing warnings/notes (``report.passed=True`` with findings):
        exits ``0`` with ``systemMessage`` only.
    - Fail-open default (``fail_closed=False``): any parse, I/O, missing file,
      unrecognized transcript, or internal error emits a single concise
      diagnostic line to stderr and exits ``0``. With ``fail_closed=True``,
      errors exit ``2``.
    """
    try:
        if mode not in ("advise", "block"):
            return _fail_result(f"unsupported hook mode: {mode!r}", fail_closed=fail_closed)

        try:
            hook_input = parse_claude_hook_input(stdin_text)
        except ValueError as exc:
            return _fail_result(str(exc), fail_closed=fail_closed)

        cwd_dir: Path | None = None
        if hook_input.cwd is not None:
            candidate_cwd = Path(hook_input.cwd).expanduser()
            if candidate_cwd.is_dir():
                cwd_dir = candidate_cwd

        effective_repo_root: Path | None
        if repo_root is not None:
            resolved_repo = repo_root.expanduser()
            if not resolved_repo.is_dir():
                return _fail_result(
                    f"repository directory not found: {resolved_repo}",
                    fail_closed=fail_closed,
                )
            effective_repo_root = resolved_repo
        else:
            effective_repo_root = cwd_dir

        transcript_path, missing_reason = _select_transcript_path(hook_input, cwd_dir)
        if transcript_path is None:
            return _fail_result(
                missing_reason or "missing transcript_path in hook payload",
                fail_closed=fail_closed,
            )
        if not transcript_path.is_file():
            return _fail_result(
                f"transcript file not found: {transcript_path}",
                fail_closed=fail_closed,
            )

        transcript = auto_parse(transcript_path)
        if not transcript.events and not transcript.assistant_messages:
            return _fail_result(
                f"empty or unrecognized transcript: {transcript_path} "
                f"(format={transcript.source_format}, "
                f"malformed_lines={transcript.malformed_lines})",
                fail_closed=fail_closed,
            )

        report = verify_transcript(
            transcript,
            repo_root=effective_repo_root,
            strict=strict,
        )

        # Clean pass: stay completely quiet.
        if report.passed and not report.findings:
            return HookResult(exit_code=0, stdout="", stderr="", blocked=False, report=report)

        feedback = format_hook_feedback(report)

        # Loop guard: when stop_hook_active is true, NEVER block (exit 2) and NEVER
        # emit additionalContext (which continues the turn in Claude Code).
        if hook_input.stop_hook_active:
            loop_guarded_feedback = (
                f"{feedback}\n\n"
                "_Note: `stop_hook_active` is true; not continuing or blocking again "
                "to prevent a stop loop._"
            )
            return HookResult(
                exit_code=0,
                stdout=_build_hook_json(
                    loop_guarded_feedback,
                    hook_input.hook_event_name,
                    include_additional_context=False,
                ),
                stderr="",
                blocked=False,
                report=report,
            )

        if mode == "block" and not report.passed:
            return HookResult(
                exit_code=2,
                stdout="",
                stderr=feedback,
                blocked=True,
                report=report,
            )

        # Advise mode on failing report: emit systemMessage + additionalContext (when event known).
        # Non-failing warning/info-only reports emit systemMessage only (no turn continuation).
        include_ctx = mode == "advise" and not report.passed
        return HookResult(
            exit_code=0,
            stdout=_build_hook_json(
                feedback,
                hook_input.hook_event_name,
                include_additional_context=include_ctx,
            ),
            stderr="",
            blocked=False,
            report=report,
        )
    except Exception as exc:
        return _fail_result(f"internal error: {exc}", fail_closed=fail_closed)


def build_claude_code_hook_command(
    *,
    mode: HookMode = "advise",
    strict: bool = False,
    command: str | None = None,
) -> str:
    """Build the shell command invoked by Claude Code hooks.

    ``command`` is treated as a shell command fragment (e.g. ``"handoff-verify"``
    or ``"uvx handoff-verify"``). If it already contains ``hook claude-code``,
    it is returned verbatim.
    """
    if mode not in ("advise", "block"):
        raise ValueError(f"Unsupported hook mode: {mode!r}")

    base = (command.strip() if command is not None else "") or "handoff-verify"

    tokens = _tokenize_command(base)
    if _contains_subsequence(tokens, ("hook", "claude-code")):
        return base

    parts = [base, "hook", "claude-code", "--mode", mode]
    if strict:
        parts.append("--strict")
    return " ".join(parts)


def _tokenize_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _is_compound_shell_command(command: str) -> bool:
    """Return True if a command contains unquoted shell control operators (&&, ||, ;, |, &)."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return any(op in command for op in ("&&", "||", ";", "|", "\n"))
    return any(tok in _SHELL_COMPOUND_OPERATORS for tok in tokens)


def _contains_subsequence(tokens: list[str], subseq: tuple[str, ...]) -> bool:
    if len(tokens) < len(subseq):
        return False
    for idx in range(len(tokens) - len(subseq) + 1):
        if tuple(tokens[idx : idx + len(subseq)]) == subseq:
            return True
    return False


def _is_handoff_verify_hook_command(command: str) -> bool:
    """Return True if a simple command invokes handoff-verify's claude-code hook."""
    if _is_compound_shell_command(command):
        return False
    tokens = _tokenize_command(command)
    if not _contains_subsequence(tokens, ("hook", "claude-code")):
        return False
    for tok in tokens:
        name = Path(tok).name.lower()
        if name in ("handoff-verify", "handoff-verify.exe", "handoff_verify", "handoff_verify.cli"):
            return True
    return False


def _matches_managed_hook(existing_command: str, target_command: str) -> bool:
    """Return True if an existing hook command should be updated in place."""
    if existing_command.strip() == target_command.strip():
        return True
    if _is_compound_shell_command(existing_command):
        return False
    if _is_handoff_verify_hook_command(existing_command):
        return True
    existing_tokens = _tokenize_command(existing_command)
    target_tokens = _tokenize_command(target_command)
    if _contains_subsequence(existing_tokens, ("hook", "claude-code")) and _contains_subsequence(
        target_tokens, ("hook", "claude-code")
    ):
        existing_prefix = existing_tokens[: existing_tokens.index("hook")]
        target_prefix = target_tokens[: target_tokens.index("hook")]
        if existing_prefix and existing_prefix == target_prefix:
            return True
    return False


def _is_global_matcher_group(entry: dict[str, Any]) -> bool:
    """Return True if a hook matcher group has no scoped matcher (global / catch-all)."""
    matcher = entry.get("matcher")
    return matcher is None or (isinstance(matcher, str) and not matcher.strip())


def build_claude_code_settings_snippet(
    *,
    mode: HookMode = "advise",
    strict: bool = False,
    command: str | None = None,
) -> dict[str, Any]:
    """Build a ready-to-paste Claude Code settings.json hooks snippet."""
    hook_cmd = build_claude_code_hook_command(mode=mode, strict=strict, command=command)
    return {
        "hooks": {
            event_name: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_cmd,
                        }
                    ]
                }
            ]
            for event_name in CLAUDE_CODE_EVENTS
        }
    }


def merge_claude_code_settings(
    existing: dict[str, Any],
    *,
    mode: HookMode = "advise",
    strict: bool = False,
    command: str | None = None,
) -> dict[str, Any]:
    """Merge handoff-verify Stop/SubagentStop hooks into a Claude settings dict.

    Preserves all unrelated keys, unrelated hooks, compound shell wrapper hooks,
    and matcher-scoped hook groups (e.g. ``{"matcher": "Explore", ...}``).
    Updates existing global (un-matchered) handoff-verify hook entries in place
    (idempotent across repeated runs) and removes duplicate global entries.
    """
    if not isinstance(existing, dict):
        raise SettingsError("settings.json root must be a JSON object")

    merged = copy.deepcopy(existing)
    hooks = merged.get("hooks")
    if hooks is None:
        hooks = {}
        merged["hooks"] = hooks
    elif not isinstance(hooks, dict):
        raise SettingsError("'hooks' in settings.json must be a JSON object")

    hook_cmd = build_claude_code_hook_command(mode=mode, strict=strict, command=command)

    for event_name in CLAUDE_CODE_EVENTS:
        event_entries = hooks.get(event_name)
        if event_entries is None:
            event_entries = []
            hooks[event_name] = event_entries
        elif not isinstance(event_entries, list):
            raise SettingsError(f"'hooks.{event_name}' in settings.json must be a JSON array")

        updated_existing = False
        new_event_entries: list[Any] = []

        for entry in event_entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("hooks"), list)
                or not _is_global_matcher_group(entry)
            ):
                # Preserve non-standard entries and matcher-scoped groups untouched.
                new_event_entries.append(entry)
                continue

            entry_hooks = entry["hooks"]
            new_handler_list: list[Any] = []
            entry_had_match = False

            for handler in entry_hooks:
                if (
                    isinstance(handler, dict)
                    and handler.get("type") == "command"
                    and isinstance(handler.get("command"), str)
                    and _matches_managed_hook(handler["command"], hook_cmd)
                ):
                    entry_had_match = True
                    if not updated_existing:
                        updated_handler = dict(handler)
                        updated_handler["command"] = hook_cmd
                        new_handler_list.append(updated_handler)
                        updated_existing = True
                    # Drop duplicate global handoff-verify handlers for this event.
                else:
                    new_handler_list.append(handler)

            if entry_had_match and not new_handler_list:
                # All handlers in this global group were duplicate handoff-verify entries.
                continue

            updated_entry = dict(entry)
            updated_entry["hooks"] = new_handler_list
            new_event_entries.append(updated_entry)

        if not updated_existing:
            new_event_entries.append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_cmd,
                        }
                    ]
                }
            )

        hooks[event_name] = new_event_entries

    return merged


def write_claude_code_settings(
    settings_path: Path,
    *,
    mode: HookMode = "advise",
    strict: bool = False,
    command: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create or safely merge handoff-verify hooks into ``settings_path`` atomically.

    Follows symlinks when writing so dotfile-managed ``settings.json`` symlinks are
    preserved rather than replaced by regular files. Preserves UTF-8 characters and
    existing file permissions (defaulting to ``0o644`` for newly created files).

    Returns ``(merged_settings, created_new)``.
    Raises ``SettingsError`` without modifying disk if existing JSON is malformed
    or has an incompatible structure.
    """
    target_path = (
        settings_path.resolve(strict=False) if settings_path.is_symlink() else settings_path
    )
    existed = target_path.exists()
    existing_mode: int | None = None
    existing_data: dict[str, Any] = {}

    if existed:
        if not target_path.is_file():
            raise SettingsError(f"settings path exists and is not a file: {settings_path}")
        try:
            existing_mode = stat.S_IMODE(target_path.stat().st_mode)
            raw_text = target_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SettingsError(f"failed to read {settings_path}: {exc}") from exc

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SettingsError(
                f"refusing to modify malformed JSON in {settings_path}: "
                f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
            ) from exc

        if not isinstance(parsed, dict):
            raise SettingsError(
                f"refusing to modify {settings_path}: root JSON value must be an object"
            )
        existing_data = parsed

    merged = merge_claude_code_settings(
        existing_data,
        mode=mode,
        strict=strict,
        command=command,
    )

    serialized = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    target_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target_path.parent,
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(tmp_path, existing_mode if existing_mode is not None else 0o644)
        os.replace(tmp_path, target_path)
    except Exception:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
        raise

    return merged, not existed

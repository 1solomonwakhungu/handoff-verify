"""Environment and hook diagnostics for `handoff-verify doctor`."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from handoff_verify import __version__
from handoff_verify.hooks import CLAUDE_CODE_EVENTS, DEFAULT_CLAUDE_SETTINGS_PATH

CheckStatus = Literal["ok", "warn", "error"]
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SHELL_BUILTINS = frozenset({"cd", "export", "env", "exec", "command", "builtin", "set", "unset"})
_SHELL_OPS = frozenset({"&&", "||", ";", "|", "|&", "&"})


@dataclass
class DoctorCheck:
    """Result of one environment or configuration check."""

    name: str
    status: CheckStatus
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "details": dict(self.details),
        }


@dataclass
class DoctorReport:
    """Full diagnostic report returned by `run_doctor()`."""

    version: str
    status: CheckStatus
    checks: list[DoctorCheck]
    next_steps: list[str]

    @property
    def has_errors(self) -> bool:
        return any(c.status == "error" for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "version": self.version,
            "status": self.status,
            "ok": not self.has_errors,
            "healthy": all(c.status == "ok" for c in self.checks),
            "checks": [c.to_dict() for c in self.checks],
            "next_steps": list(self.next_steps),
        }


def _extract_hook_executable(command: str) -> str | None:
    """Extract the executable token invoking handoff-verify, skipping env vars and shell ops."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()

    # Split into simple command segments on shell operators and prefer the segment that
    # mentions handoff-verify (so `cd dir && FOO=1 handoff-verify hook ...` checks `handoff-verify`,
    # never `cd` or `FOO=1`).
    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SHELL_OPS:
            if cur:
                segments.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)

    target_seg: list[str] | None = None
    for seg in segments:
        # Skip `cd /path/to/handoff-verify` segments even though the path contains the name.
        first_cmd = next((t for t in seg if not _ENV_ASSIGN_RE.match(t)), "")
        if first_cmd == "cd":
            continue
        if any("handoff-verify" in t.lower() or "handoff_verify" in t.lower() for t in seg):
            target_seg = seg
            break
    if target_seg is None:
        target_seg = segments[0] if segments else []

    idx = 0
    while idx < len(target_seg):
        tok = target_seg[idx]
        if _ENV_ASSIGN_RE.match(tok):
            idx += 1
            continue
        if tok in _SHELL_BUILTINS:
            idx += 1
            # Skip flags to `env` / `command`
            while idx < len(target_seg) and target_seg[idx].startswith("-"):
                idx += 1
            continue
        return tok
    return None


def _is_hv_hook_entry(command: str) -> bool:
    low = command.lower()
    return "handoff-verify" in low or "handoff_verify" in low


def _inspect_settings_file(path: Path) -> dict[str, Any]:
    """Inspect a Claude Code settings.json file for handoff-verify hooks."""
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "valid_json": None,
        "error": None,
        "events_configured": [],
        "commands": {},
        "missing_executables": [],
    }
    if not path.is_file():
        return info

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        info["valid_json"] = False
        info["error"] = str(exc)
        return info

    if not isinstance(data, dict):
        info["valid_json"] = False
        info["error"] = "root JSON value is not an object"
        return info

    info["valid_json"] = True
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return info

    for event_name in CLAUDE_CODE_EVENTS:
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        found_cmds: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            handlers = entry.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if (
                    isinstance(handler, dict)
                    and handler.get("type") == "command"
                    and isinstance(handler.get("command"), str)
                    and _is_hv_hook_entry(handler["command"])
                ):
                    cmd_str = handler["command"].strip()
                    found_cmds.append(cmd_str)
                    exe = _extract_hook_executable(cmd_str)
                    if exe and "${" not in exe and "$" not in exe:
                        exe_path = Path(exe).expanduser()
                        if exe_path.is_absolute():
                            resolves = exe_path.is_file()
                        elif "/" in exe or "\\" in exe:
                            # Relative path resolves against project root (parent of .claude) or cwd
                            project_base = (
                                path.parent.parent if path.parent.name == ".claude" else path.parent
                            )
                            resolves = (project_base / exe_path).is_file() or exe_path.is_file()
                        else:
                            resolves = shutil.which(exe) is not None
                        if not resolves and exe not in info["missing_executables"]:
                            info["missing_executables"].append(exe)
        if found_cmds:
            info["events_configured"].append(event_name)
            info["commands"][event_name] = found_cmds

    return info


def _detect_claude_plugin(claude_dirs: list[Path]) -> Path | None:
    """Return installed_plugins.json path if handoff-verify is registered as a Claude plugin."""
    for base in claude_dirs:
        installed = base / "plugins" / "installed_plugins.json"
        if not installed.is_file():
            continue
        try:
            raw = installed.read_text(encoding="utf-8")
            if "handoff-verify" not in raw:
                continue
            data = json.loads(raw)
            if not isinstance(data, dict):
                continue
            plugins = data.get("plugins", data)
            if isinstance(plugins, dict):
                for key, val in plugins.items():
                    if "handoff-verify" not in key:
                        continue
                    if isinstance(val, dict) and val.get("enabled") is False:
                        continue
                    if isinstance(val, list):
                        active = [
                            item
                            for item in val
                            if not (isinstance(item, dict) and item.get("enabled") is False)
                        ]
                        if not active:
                            continue
                    return installed
            elif isinstance(plugins, list):
                for item in plugins:
                    if (
                        isinstance(item, dict)
                        and "handoff-verify" in str(item.get("name", ""))
                        and item.get("enabled") is not False
                    ):
                        return installed
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


def _claude_config_dirs() -> list[Path]:
    dirs: list[Path] = []
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        dirs.append(Path(env_dir).expanduser())
    default_dir = Path.home() / ".claude"
    if default_dir not in dirs:
        dirs.append(default_dir)
    return dirs


def _find_project_settings_files(effective_cwd: Path) -> list[Path]:
    """Locate project `.claude/settings.json` and `settings.local.json`, walking up to git root."""
    candidates: list[Path] = []
    search_dirs: list[Path] = [effective_cwd]
    # Walk up to repo root (stopping at first `.git` directory/file)
    cur = effective_cwd
    for _ in range(12):
        if (cur / ".git").exists():
            break
        parent = cur.parent
        if parent == cur:
            break
        search_dirs.append(parent)
        cur = parent

    for directory in search_dirs:
        for rel in (DEFAULT_CLAUDE_SETTINGS_PATH, Path(".claude/settings.local.json")):
            cand = directory / rel
            if cand.is_file() and cand not in candidates:
                candidates.append(cand)
        if candidates:
            break

    if not candidates:
        candidates.append(effective_cwd / DEFAULT_CLAUDE_SETTINGS_PATH)
    return candidates


def run_doctor(
    *,
    cwd: Path | None = None,
    settings_path: Path | None = None,
    python_version: tuple[int, int, int] | None = None,
) -> DoctorReport:
    """Run installation and hook readiness diagnostics."""
    effective_cwd = (cwd or Path.cwd()).expanduser()
    py_ver = python_version or (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    checks: list[DoctorCheck] = []
    next_steps: list[str] = []

    # 1. Python version check
    py_str = f"{py_ver[0]}.{py_ver[1]}.{py_ver[2]}"
    if py_ver >= (3, 11, 0):
        checks.append(
            DoctorCheck(
                name="python_version",
                status="ok",
                summary=f"Python {py_str} ({sys.executable})",
                details={"version": py_str, "executable": sys.executable, "required": ">=3.11"},
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="python_version",
                status="error",
                summary=f"Python {py_str} is older than required >=3.11",
                details={"version": py_str, "executable": sys.executable, "required": ">=3.11"},
            )
        )
        next_steps.append("Install Python 3.11+ (e.g. `uv python install 3.13`).")

    # 2. Binary on PATH check
    resolved_bin = shutil.which("handoff-verify")
    if resolved_bin:
        checks.append(
            DoctorCheck(
                name="binary_on_path",
                status="ok",
                summary=f"`handoff-verify` found on PATH at {resolved_bin}",
                details={"found": True, "path": resolved_bin},
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="binary_on_path",
                status="warn",
                summary="`handoff-verify` is not on PATH",
                details={"found": False, "path": None},
            )
        )
        next_steps.append(
            "Install `handoff-verify` onto PATH (`uv tool install handoff-verify`, or "
            "`uv tool install .` from a local clone) so Claude Code hooks can invoke it."
        )

    # 3. Claude Code hook configuration check
    candidate_settings: list[Path] = []
    if settings_path is not None:
        candidate_settings.append(settings_path.expanduser())
    else:
        candidate_settings.extend(_find_project_settings_files(effective_cwd))
        for cfg_dir in _claude_config_dirs():
            for name in ("settings.json", "settings.local.json"):
                user_settings = cfg_dir / name
                if (
                    name == "settings.json" or user_settings.is_file()
                ) and user_settings not in candidate_settings:
                    candidate_settings.append(user_settings)

    inspected = [_inspect_settings_file(p) for p in candidate_settings]
    plugin_path = _detect_claude_plugin(_claude_config_dirs()) if settings_path is None else None

    malformed = [item for item in inspected if item["exists"] and item["valid_json"] is False]
    missing_execs = [
        (item["path"], exe) for item in inspected for exe in item["missing_executables"]
    ]
    configured_events: set[str] = set()
    configured_files: list[str] = []
    for item in inspected:
        if item["events_configured"]:
            configured_events.update(item["events_configured"])
            configured_files.append(item["path"])

    expected_events = set(CLAUDE_CODE_EVENTS)
    hook_details: dict[str, Any] = {
        "settings_checked": inspected,
        "configured_events": sorted(configured_events),
        "plugin_installed": plugin_path is not None,
        "plugin_registry": str(plugin_path) if plugin_path else None,
        "duplicate_sources": bool(configured_files and plugin_path is not None),
    }

    if malformed:
        bad = malformed[0]
        checks.append(
            DoctorCheck(
                name="claude_hooks",
                status="error",
                summary=f"Malformed Claude settings file: {bad['path']} ({bad['error']})",
                details=hook_details,
            )
        )
        next_steps.append(
            f"Fix invalid JSON in {bad['path']} before running `handoff-verify init --write`."
        )
    elif missing_execs:
        bad_file, bad_exe = missing_execs[0]
        checks.append(
            DoctorCheck(
                name="claude_hooks",
                status="error",
                summary=(
                    f"Configured hook in {bad_file} invokes `{bad_exe}`, which is not on PATH"
                ),
                details=hook_details,
            )
        )
        next_steps.append(
            f"Put `{bad_exe}` on PATH or update {bad_file} with "
            '`handoff-verify init --write --command "<absolute path or launcher>"`.'
        )
    elif configured_files and plugin_path is not None:
        sources = ", ".join(configured_files)
        checks.append(
            DoctorCheck(
                name="claude_hooks",
                status="warn",
                summary=(
                    f"Duplicate hook configuration: both settings ({sources}) and Claude plugin "
                    f"({plugin_path}) are active"
                ),
                details=hook_details,
            )
        )
        next_steps.append(
            "Remove the manual hook entries from settings.json or uninstall the plugin so hooks "
            "do not run twice on every Stop."
        )
    elif expected_events.issubset(configured_events):
        sources = ", ".join(configured_files)
        checks.append(
            DoctorCheck(
                name="claude_hooks",
                status="ok",
                summary=f"Stop and SubagentStop hooks configured in {sources}",
                details=hook_details,
            )
        )
    elif plugin_path is not None:
        checks.append(
            DoctorCheck(
                name="claude_hooks",
                status="ok",
                summary=f"Claude Code plugin installed ({plugin_path})",
                details=hook_details,
            )
        )
    elif configured_events:
        missing_ev = sorted(expected_events - configured_events)
        checks.append(
            DoctorCheck(
                name="claude_hooks",
                status="warn",
                summary=(
                    f"Only {', '.join(sorted(configured_events))} hook configured "
                    f"(missing {', '.join(missing_ev)})"
                ),
                details=hook_details,
            )
        )
        next_steps.append(
            "Run `handoff-verify init --write` to configure both SubagentStop and Stop hooks."
        )
    else:
        checked_str = ", ".join(str(p) for p in candidate_settings)
        checks.append(
            DoctorCheck(
                name="claude_hooks",
                status="warn",
                summary=f"No Claude Code hooks configured (checked {checked_str})",
                details=hook_details,
            )
        )
        next_steps.append(
            "Run `handoff-verify init --write` for this repo (or "
            "`handoff-verify init --write --settings ~/.claude/settings.json` for all repos)."
        )

    if any(c.status == "error" for c in checks):
        overall: CheckStatus = "error"
    elif any(c.status == "warn" for c in checks):
        overall = "warn"
    else:
        overall = "ok"

    return DoctorReport(
        version=__version__,
        status=overall,
        checks=checks,
        next_steps=next_steps,
    )


def format_doctor_text(report: DoctorReport) -> str:
    """Format a DoctorReport as a concise, actionable plain-text checklist."""
    label_map = {"ok": "OK", "warn": "WARN", "error": "ERROR"}
    header_map = {
        "ok": "READY",
        "warn": "NEEDS SETUP",
        "error": "ISSUES FOUND",
    }
    lines = [f"handoff-verify doctor ({report.version}): {header_map[report.status]}"]
    for check in report.checks:
        lines.append(f"  [{label_map[check.status]}] {check.name}: {check.summary}")
    if report.next_steps:
        lines.append("")
        lines.append("Next steps:")
        for step in report.next_steps:
            lines.append(f"  - {step}")
    return "\n".join(lines)


def format_doctor_json(report: DoctorReport) -> str:
    """Format a DoctorReport as stable indented JSON."""
    return json.dumps(report.to_dict(), indent=2)

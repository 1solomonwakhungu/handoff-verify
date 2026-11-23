"""Environment and hook diagnostics for `handoff-verify doctor`."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from handoff_verify import __version__
from handoff_verify.hooks import CLAUDE_CODE_EVENTS, DEFAULT_CLAUDE_SETTINGS_PATH

CheckStatus = Literal["ok", "warn", "error"]


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
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return tokens[0] if tokens else None


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
                        resolves = exe_path.is_file() or shutil.which(exe) is not None
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
            if "handoff-verify" in raw:
                data = json.loads(raw)
                if isinstance(data, dict):
                    plugins = data.get("plugins", data)
                    if isinstance(plugins, dict) and any("handoff-verify" in k for k in plugins):
                        return installed
                    if isinstance(plugins, list) and any(
                        isinstance(p, dict) and "handoff-verify" in str(p.get("name", ""))
                        for p in plugins
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
        candidate_settings.append(effective_cwd / DEFAULT_CLAUDE_SETTINGS_PATH)
        for cfg_dir in _claude_config_dirs():
            user_settings = cfg_dir / "settings.json"
            if user_settings not in candidate_settings:
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

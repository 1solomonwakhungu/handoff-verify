"""Output formatters for handoff-verify verification reports.

Provides quiet-when-clean and loud-on-anomalies renderers in plain text,
Markdown, machine-readable JSON, and compact hook feedback formats.
"""

from __future__ import annotations

import json
from typing import Any

from handoff_verify.models import Claim, Finding, ScopeReceipt, VerificationReport

_SEVERITY_ORDER: dict[str, int] = {
    "error": 0,
    "warning": 1,
    "info": 2,
}

_MAX_INLINE_LEN = 180
_MAX_EVIDENCE_ITEMS = 6
_MAX_EVIDENCE_LIST_ITEMS = 5


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def _truncate_inline(text: str, max_len: int = _MAX_INLINE_LEN) -> str:
    """Collapse whitespace to a single line and truncate cleanly if long."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 3] + "..."


def _md_code(text: str, max_len: int = _MAX_INLINE_LEN) -> str:
    """Wrap text safely in Markdown inline code backticks."""
    clean = _truncate_inline(text, max_len=max_len)
    # Choose a backtick fence longer than any consecutive backticks inside clean.
    longest_run = 0
    current_run = 0
    for ch in clean:
        if ch == "`":
            current_run += 1
            if current_run > longest_run:
                longest_run = current_run
        else:
            current_run = 0
    fence = "`" * (longest_run + 1)
    if clean.startswith("`") or clean.endswith("`") or clean.startswith(" ") or clean.endswith(" "):
        return f"{fence} {clean} {fence}"
    return f"{fence}{clean}{fence}"


def _sort_findings(findings: list[Finding]) -> list[Finding]:
    """Return findings ordered by severity (error, warning, info), preserving tie order."""
    return sorted(findings, key=lambda f: _SEVERITY_ORDER.get(f.severity, 99))


def _count_severities(findings: list[Finding]) -> tuple[int, int, int]:
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")
    infos = sum(1 for f in findings if f.severity == "info")
    return errors, warnings, infos


def _severity_breakdown(findings: list[Finding]) -> str:
    errors, warnings, infos = _count_severities(findings)
    parts: list[str] = []
    if errors:
        parts.append(_plural(errors, "error"))
    if warnings:
        parts.append(_plural(warnings, "warning"))
    if infos:
        parts.append(_plural(infos, "info"))
    return ", ".join(parts)


def _format_evidence_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _truncate_inline(value, max_len=80)
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        shown = [_format_evidence_value(v) for v in value[:_MAX_EVIDENCE_LIST_ITEMS]]
        shown = [s for s in shown if s]
        extra = len(value) - _MAX_EVIDENCE_LIST_ITEMS
        joined = ", ".join(shown)
        if extra > 0:
            return f"[{joined}, +{extra} more]"
        return f"[{joined}]"
    if isinstance(value, dict):
        if not value:
            return ""
        items = []
        for k, v in list(value.items())[:4]:
            rendered = _format_evidence_value(v)
            if rendered:
                items.append(f"{k}={rendered}")
        if len(value) > 4:
            items.append(f"+{len(value) - 4} more")
        return "{" + ", ".join(items) + "}"
    return _truncate_inline(str(value), max_len=80)


def _format_evidence_summary(evidence: dict[str, Any]) -> str:
    """Format a finding's evidence dictionary as a concise, deterministic string."""
    if not evidence:
        return ""
    parts: list[str] = []
    items = list(evidence.items())
    for key, val in items[:_MAX_EVIDENCE_ITEMS]:
        if val is None or val == "" or val == [] or val == {}:
            continue
        rendered = _format_evidence_value(val)
        if rendered:
            parts.append(f"{key}={rendered}")
    if len(items) > _MAX_EVIDENCE_ITEMS:
        parts.append(f"+{len(items) - _MAX_EVIDENCE_ITEMS} more")
    return "; ".join(parts)


def _format_claim_location(claim: Claim) -> str:
    if not claim.file_path:
        return ""
    if claim.start_line is not None and claim.end_line is not None:
        if claim.end_line != claim.start_line:
            return f"{claim.file_path}:{claim.start_line}-{claim.end_line}"
        return f"{claim.file_path}:{claim.start_line}"
    if claim.start_line is not None:
        return f"{claim.file_path}:{claim.start_line}"
    return claim.file_path


def _has_relevant_scope_receipt(scope: ScopeReceipt) -> bool:
    return bool(
        scope.searched_roots
        or scope.excluded_dirs
        or scope.truncated_outputs
        or scope.unread_cursors
        or scope.files_edited
    )


def _format_truncated_item(item: dict[str, Any], *, markdown: bool) -> str:
    idx = item.get("event_index", "?")
    tool = item.get("tool_name") or "tool"
    reason = item.get("reason") or "truncated"
    if markdown:
        return f"`[{idx}] {tool}` ({_truncate_inline(str(reason), 80)})"
    return f"[{idx}] {tool} ({_truncate_inline(str(reason), 80)})"


def _format_cursor_item(item: dict[str, Any], *, markdown: bool) -> str:
    idx = item.get("event_index", "?")
    tool = item.get("tool_name") or "tool"
    resource = item.get("resource") or ""
    page = item.get("page")
    total_pages = item.get("total_pages")
    cursor = item.get("next_cursor")

    details: list[str] = []
    if resource:
        details.append(_md_code(str(resource), 60) if markdown else str(resource))
    if page is not None and total_pages is not None:
        details.append(f"page {page}/{total_pages}")
    elif page is not None:
        details.append(f"page {page}")
    if cursor:
        details.append(f"cursor {_md_code(str(cursor), 40)}" if markdown else f"cursor={cursor}")

    head = f"`[{idx}] {tool}`" if markdown else f"[{idx}] {tool}"
    if details:
        return f"{head} ({', '.join(details)})"
    return head


def _scope_receipt_lines(scope: ScopeReceipt, *, markdown: bool) -> list[str]:
    """Render relevant ScopeReceipt details in text or Markdown."""
    if not _has_relevant_scope_receipt(scope):
        return []

    lines: list[str] = []
    if scope.searched_roots:
        roots = ", ".join(_md_code(r, 80) if markdown else r for r in scope.searched_roots[:10])
        if len(scope.searched_roots) > 10:
            roots += f", +{len(scope.searched_roots) - 10} more"
        lines.append(f"- **Searched roots:** {roots}" if markdown else f"  Searched roots: {roots}")

    if scope.excluded_dirs:
        excluded = ", ".join(_md_code(d, 80) if markdown else d for d in scope.excluded_dirs[:10])
        if len(scope.excluded_dirs) > 10:
            excluded += f", +{len(scope.excluded_dirs) - 10} more"
        lines.append(
            f"- **Excluded dirs:** {excluded}" if markdown else f"  Excluded dirs: {excluded}"
        )

    if scope.truncated_outputs:
        items = ", ".join(
            _format_truncated_item(item, markdown=markdown) for item in scope.truncated_outputs[:5]
        )
        if len(scope.truncated_outputs) > 5:
            items += f", +{len(scope.truncated_outputs) - 5} more"
        count = len(scope.truncated_outputs)
        lines.append(
            f"- **Truncated outputs ({count}):** {items}"
            if markdown
            else f"  Truncated outputs ({count}): {items}"
        )

    if scope.unread_cursors:
        items = ", ".join(
            _format_cursor_item(item, markdown=markdown) for item in scope.unread_cursors[:5]
        )
        if len(scope.unread_cursors) > 5:
            items += f", +{len(scope.unread_cursors) - 5} more"
        count = len(scope.unread_cursors)
        lines.append(
            f"- **Unread pagination cursors ({count}):** {items}"
            if markdown
            else f"  Unread pagination cursors ({count}): {items}"
        )

    if scope.files_edited:
        edited = ", ".join(_md_code(f, 80) if markdown else f for f in scope.files_edited[:10])
        if len(scope.files_edited) > 10:
            edited += f", +{len(scope.files_edited) - 10} more"
        lines.append(f"- **Files edited:** {edited}" if markdown else f"  Files edited: {edited}")

    return lines


def _clean_summary(report: VerificationReport) -> str:
    return (
        f"0 findings ("
        f"{_plural(len(report.claims), 'claim')} checked, "
        f"{_plural(len(report.scope_receipt.files_read), 'file')} read, "
        f"{report.duration_ms:.1f}ms)"
    )


def report_to_json_dict(report: VerificationReport) -> dict[str, Any]:
    """Convert a VerificationReport to the stable machine-readable JSON schema."""
    errors, warnings, infos = _count_severities(report.findings)
    return {
        "schema_version": report.schema_version,
        "passed": report.passed,
        "transcript_path": report.transcript_path,
        "source_format": report.source_format,
        "duration_ms": report.duration_ms,
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "infos": infos,
            "claims": len(report.claims),
            "findings": len(report.findings),
        },
        "claims": [claim.to_dict() for claim in report.claims],
        "findings": [finding.to_dict() for finding in report.findings],
        "scope_receipt": report.scope_receipt.to_dict(),
    }


def format_json(report: VerificationReport) -> str:
    """Format a VerificationReport as stable, indented JSON."""
    return json.dumps(report_to_json_dict(report), indent=2)


def format_text(report: VerificationReport, *, quiet_on_pass: bool = False) -> str:
    """Format a VerificationReport as plain text.

    Quiet when clean: returns empty string when ``quiet_on_pass=True`` and clean,
    or a single concise summary line otherwise.
    """
    is_clean = report.passed and not report.findings
    if is_clean:
        if quiet_on_pass:
            return ""
        return f"PASS: {_clean_summary(report)}"

    status = "PASS (warnings)" if report.passed else "FAIL"
    breakdown = _severity_breakdown(report.findings)
    breakdown_segment = f"{breakdown}; " if breakdown else ""
    header = (
        f"{status}: {_plural(len(report.findings), 'finding')} "
        f"({breakdown_segment}"
        f"{_plural(len(report.claims), 'claim')} checked, "
        f"{_plural(len(report.scope_receipt.files_read), 'file')} read, "
        f"{report.duration_ms:.1f}ms)"
    )

    lines = [header]
    for finding in _sort_findings(report.findings):
        lines.append(f"  [{finding.severity.upper()}] {finding.code}: {finding.message}")
        if finding.claim is not None:
            claim_text = _truncate_inline(finding.claim.text)
            loc = _format_claim_location(finding.claim)
            loc_suffix = f" ({loc})" if loc and loc not in claim_text else ""
            lines.append(f"    Claim ({finding.claim.kind}): {claim_text}{loc_suffix}")
        evidence_text = _format_evidence_summary(finding.evidence)
        if evidence_text:
            lines.append(f"    Evidence: {evidence_text}")
        if finding.suggestion:
            lines.append(f"    Next step: {finding.suggestion}")

    scope_lines = _scope_receipt_lines(report.scope_receipt, markdown=False)
    if scope_lines:
        lines.append("Scope receipt:")
        lines.extend(scope_lines)

    return "\n".join(lines)


def format_markdown(report: VerificationReport, *, quiet_on_pass: bool = False) -> str:
    """Format a VerificationReport as scannable Markdown for humans and parent agents."""
    is_clean = report.passed and not report.findings
    if is_clean:
        if quiet_on_pass:
            return ""
        return f"**handoff-verify:** PASS — {_clean_summary(report)}"

    status = "PASS (warnings)" if report.passed else "FAIL"
    breakdown = _severity_breakdown(report.findings)
    breakdown_part = f" ({breakdown})" if breakdown else ""
    summary_line = (
        f"**Summary:** {_plural(len(report.findings), 'finding')}{breakdown_part} · "
        f"{_plural(len(report.claims), 'claim')} checked · "
        f"{_plural(len(report.scope_receipt.files_read), 'file')} read · "
        f"{report.duration_ms:.1f}ms"
    )

    lines = [
        f"### handoff-verify: {status}",
        "",
        summary_line,
        "",
        "#### Findings",
        "",
    ]

    for finding in _sort_findings(report.findings):
        lines.append(f"- **[{finding.severity.upper()}] `{finding.code}`** — {finding.message}")
        if finding.claim is not None:
            claim_code = _md_code(finding.claim.text)
            loc = _format_claim_location(finding.claim)
            loc_suffix = f" ({_md_code(loc)})" if loc and loc not in finding.claim.text else ""
            lines.append(f"  - **Claim (`{finding.claim.kind}`):** {claim_code}{loc_suffix}")
        evidence_text = _format_evidence_summary(finding.evidence)
        if evidence_text:
            lines.append(f"  - **Evidence:** {evidence_text}")
        if finding.suggestion:
            lines.append(f"  - **Next step:** {finding.suggestion}")

    scope_lines = _scope_receipt_lines(report.scope_receipt, markdown=True)
    if scope_lines:
        lines.extend(["", "#### Scope Receipt", ""])
        lines.extend(scope_lines)

    return "\n".join(lines)


def format_hook_feedback(report: VerificationReport, *, max_findings: int = 10) -> str:
    """Format a compact, high-signal Markdown receipt for hook stderr / agent context.

    Returns an empty string when verification is clean (passed with zero findings).
    """
    if report.passed and not report.findings:
        return ""

    status = "WARN" if report.passed else "FAIL"
    breakdown = _severity_breakdown(report.findings)
    breakdown_part = f" ({breakdown})" if breakdown else ""
    lines = [
        f"**handoff-verify: {status}** — "
        f"{_plural(len(report.findings), 'finding')}{breakdown_part} "
        f"across {_plural(len(report.claims), 'claim')} checked:",
    ]

    sorted_findings = _sort_findings(report.findings)
    shown = sorted_findings[:max_findings] if max_findings > 0 else sorted_findings
    for finding in shown:
        claim_part = ""
        if finding.claim is not None:
            claim_part = f" ({_md_code(finding.claim.text, max_len=100)})"
        lines.append(
            f"- **[{finding.severity.upper()}] `{finding.code}`**{claim_part}: {finding.message}"
        )
        evidence_text = _format_evidence_summary(finding.evidence)
        if evidence_text:
            lines.append(f"  - Evidence: {evidence_text}")
        if finding.suggestion:
            lines.append(f"  - Next step: {finding.suggestion}")

    hidden_count = len(sorted_findings) - len(shown)
    if hidden_count > 0:
        lines.append(f"- ... and {_plural(hidden_count, 'more finding')} omitted.")

    scope_lines = _scope_receipt_lines(report.scope_receipt, markdown=True)
    if scope_lines:
        lines.append("")
        lines.append("**Scope receipt:**")
        lines.extend(scope_lines)

    return "\n".join(lines)


def format_report(
    report: VerificationReport,
    fmt: str = "text",
    *,
    quiet_on_pass: bool = False,
) -> str:
    """Dispatch report formatting to text, markdown, or json."""
    if fmt not in ("text", "markdown", "json"):
        raise ValueError(f"Unsupported output format: {fmt!r}")

    if quiet_on_pass and report.passed and not report.findings:
        return ""

    if fmt == "json":
        return format_json(report)
    if fmt == "markdown":
        return format_markdown(report, quiet_on_pass=quiet_on_pass)
    return format_text(report, quiet_on_pass=quiet_on_pass)

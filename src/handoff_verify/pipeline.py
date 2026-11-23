"""Verification pipeline entry point and scope receipt builder."""

from __future__ import annotations

import time
from pathlib import Path

from handoff_verify.extractors import extract_claims
from handoff_verify.models import (
    Finding,
    ParsedTranscript,
    ScopeReceipt,
    VerificationReport,
)
from handoff_verify.verifiers import verify_pointers, verify_quotes, verify_scope


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_scope_receipt(transcript: ParsedTranscript) -> ScopeReceipt:
    """Build a factual summary of searched roots, exclusions, truncations, and unread cursors."""
    searched_roots: list[str] = []
    excluded_dirs: list[str] = []
    for search in transcript.searches:
        if not search.has_result or search.is_error:
            continue
        if search.search_paths:
            searched_roots.extend(search.search_paths)
        elif search.search_path:
            searched_roots.append(search.search_path)
        else:
            searched_roots.append(".")
        excluded_dirs.extend(search.excluded_dirs)

    truncated_outputs: list[dict[str, object]] = []
    seen_trunc_indices: set[int] = set()
    for ev in (
        list(transcript.events)
        + list(transcript.searches)
        + list(transcript.file_reads)
        + list(transcript.commands)
    ):
        if (
            ev.has_result
            and not ev.is_error
            and ev.truncated
            and ev.event_index not in seen_trunc_indices
        ):
            seen_trunc_indices.add(ev.event_index)
            truncated_outputs.append(
                {
                    "event_index": ev.event_index,
                    "tool_name": ev.tool_name,
                    "reason": ev.truncation_reason or "truncated",
                }
            )
    truncated_outputs.sort(key=lambda item: int(item["event_index"]))  # type: ignore[arg-type]

    unread_cursors: list[dict[str, object]] = []
    for pag in transcript.paginations:
        if pag.has_result and not pag.is_error and pag.has_more and not pag.cursor_followed:
            unread_cursors.append(
                {
                    "event_index": pag.event_index,
                    "tool_name": pag.tool_name,
                    "resource": pag.resource,
                    "next_cursor": pag.next_cursor,
                    "page": pag.page,
                    "total_pages": pag.total_pages,
                }
            )

    files_read = _dedupe_preserve_order(
        [
            r.file_path
            for r in transcript.file_reads
            if r.file_path and r.has_result and not r.is_error
        ]
    )
    files_edited = _dedupe_preserve_order(
        [
            e.file_path
            for e in transcript.file_edits
            if e.file_path and e.has_result and not e.is_error
        ]
    )

    return ScopeReceipt(
        searched_roots=_dedupe_preserve_order(searched_roots),
        excluded_dirs=_dedupe_preserve_order(excluded_dirs),
        truncated_outputs=truncated_outputs,
        unread_cursors=unread_cursors,
        files_read=files_read,
        files_edited=files_edited,
    )


def verify_transcript(
    transcript: ParsedTranscript,
    *,
    repo_root: Path | None = None,
    strict: bool = False,
) -> VerificationReport:
    """Extract and deterministically verify claims in a parsed transcript."""
    t0 = time.perf_counter()

    scope_receipt = build_scope_receipt(transcript)
    claims = extract_claims(transcript, repo_root=repo_root)

    findings: list[Finding] = []
    findings.extend(verify_pointers(claims, transcript, repo_root=repo_root))
    findings.extend(verify_quotes(claims, transcript, repo_root=repo_root))
    findings.extend(verify_scope(claims, transcript, scope_receipt, repo_root=repo_root))

    passed = not any(
        f.severity == "error" or (strict and f.severity == "warning") for f in findings
    )
    duration_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    return VerificationReport(
        schema_version="1.0",
        transcript_path=transcript.transcript_path,
        source_format=transcript.source_format,
        claims=claims,
        findings=findings,
        scope_receipt=scope_receipt,
        passed=passed,
        duration_ms=duration_ms,
    )

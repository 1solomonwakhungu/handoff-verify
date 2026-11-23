"""Absence and completeness claim verifier against transcript search & pagination receipts."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from handoff_verify.models import (
    Claim,
    Finding,
    ParsedTranscript,
    ScopeReceipt,
    SearchEvent,
)
from handoff_verify.verifiers._paths import (
    build_base_dirs,
    is_repo_root_path,
    normalize_path,
    path_is_within,
    paths_match,
)

# Standard VCS, dependency, and build/cache directories routinely excluded from searches.
_TRIVIAL_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".env",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
        ".eggs",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".idea",
        ".vscode",
        "coverage",
        "htmlcov",
        ".coverage",
        ".ds_store",
    }
)

_LISTING_ONLY_TOOLS: frozenset[str] = frozenset(
    {"glob", "ls", "list_dir", "list_files", "find", "fd"}
)

_PAGINATION_CLAIM_RE = re.compile(
    r"\b(?:pages?|pagination|cursors?|issues?|prs?|pull\s+requests?|results?|items?|"
    r"records?|entries|tickets?|comments?|commits?|branches?|logs?|all\s+\d+)\b",
    re.IGNORECASE,
)

_FILE_OR_CODE_SEARCH_CLAIM_RE = re.compile(
    r"\b(?:callers?|references?|usages?|uses|occurrences?|importers?|consumers?|"
    r"implementations?|definitions?|codebase|repo|repository|files?|todos?|fixmes?|"
    r"anywhere|nowhere\s+else|matches)\b",
    re.IGNORECASE,
)

_EXCLUSION_ACKNOWLEDGED_RE = re.compile(
    r"\b(?:exclud(?:ing|ed)|outside(?:\s+of)?|other\s+than|apart\s+from|ignoring|except)\b",
    re.IGNORECASE,
)


def _normalize_excluded_dir(raw: str) -> str:
    s = raw.strip().replace("\\", "/").lstrip("!").strip("/")
    for suffix in ("/**", "/*"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.strip("/")


def _is_trivial_excluded_dir(raw: str) -> bool:
    norm = _normalize_excluded_dir(raw).lower()
    if not norm:
        return True
    if norm in _TRIVIAL_EXCLUDED_DIRS:
        return True
    base = posixpath.basename(norm)
    if base in _TRIVIAL_EXCLUDED_DIRS:
        return True
    return base.startswith("*.") and base in ("*.pyc", "*.pyo", "*.min.js", "*.map", "*.lock")


def _search_roots_for_event(search: SearchEvent) -> list[str]:
    if search.search_paths:
        return list(search.search_paths)
    if search.search_path:
        return [search.search_path]
    return ["."]


def _looks_like_file_path(scope: str) -> bool:
    base = posixpath.basename(scope.rstrip("/"))
    return "." in base and not base.startswith(".")


def _truncation_resolved_by_later_search(
    trunc_search: SearchEvent,
    all_searches: list[SearchEvent],
    base_dirs: tuple[str, ...],
) -> bool:
    """Return True if a later non-truncated search superseded a truncated search."""
    trunc_roots = _search_roots_for_event(trunc_search)
    for later in all_searches:
        if later.event_index <= trunc_search.event_index:
            continue
        if not later.has_result or later.is_error or later.truncated:
            continue
        # Must match the same query (or trunc_search had no query)
        if trunc_search.query and later.query != trunc_search.query:
            continue
        later_roots = _search_roots_for_event(later)
        # Every root in trunc_search must be covered by some root in later
        if all(
            any(path_is_within(tr, lr, base_dirs=base_dirs) for lr in later_roots)
            for tr in trunc_roots
        ):
            return True
    return False


def _pagination_applies_to_claim(
    claim: Claim,
    unread_cursors: list[dict[str, object]],
    has_code_searches: bool,
) -> bool:
    """Return True if unread pagination cursors contradict this absence/completeness claim."""
    if not unread_cursors:
        return False
    combined = f"{claim.text} {claim.context}".lower()
    # Explicitly local-file-scoped claim doesn't contradict an unrelated API pagination cursor
    if claim.file_path and not _PAGINATION_CLAIM_RE.search(combined):
        return False
    if claim.kind == "completeness" and (
        _PAGINATION_CLAIM_RE.search(combined) or not has_code_searches
    ):
        # Completeness claims like "reviewed all pages/results/items" or "checked all issues"
        return True
    if claim.kind == "absence":
        if _PAGINATION_CLAIM_RE.search(combined):
            return True
        for cur in unread_cursors:
            resource = str(cur.get("resource") or "").lower()
            if resource and len(resource) >= 3 and resource in combined:
                return True
        if not has_code_searches:
            return True
    return False


def _format_cursor_suggestion(unread_cursors: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for cur in unread_cursors[:3]:
        tool = str(cur.get("tool_name") or "tool")
        resource = str(cur.get("resource") or "")
        next_cursor = cur.get("next_cursor")
        page = cur.get("page")
        total_pages = cur.get("total_pages")
        detail_bits: list[str] = []
        if next_cursor:
            detail_bits.append(f"cursor={next_cursor!r}")
        if page is not None and total_pages is not None:
            detail_bits.append(f"page {int(page) + 1} of {total_pages}")  # type: ignore[arg-type]
        elif page is not None:
            detail_bits.append(f"after page {page}")
        label = f"{tool} ({resource})" if resource and resource != tool else tool
        if detail_bits:
            parts.append(f"{label} [{', '.join(detail_bits)}]")
        else:
            parts.append(label)
    return (
        f"Fetch remaining paginated results from {', '.join(parts)} before claiming completeness."
    )


def verify_scope(
    claims: list[Claim],
    transcript: ParsedTranscript,
    scope_receipt: ScopeReceipt,
    *,
    repo_root: Path | None = None,
) -> list[Finding]:
    """Verify absence and completeness claims against search, read, and pagination evidence."""
    scope_claims = [c for c in claims if c.kind in ("absence", "completeness")]
    if not scope_claims:
        return []

    from handoff_verify.extractors import extract_scope_qualifiers

    base_dirs = build_base_dirs(transcript.cwd, repo_root)
    valid_searches = [s for s in transcript.searches if s.has_result and not s.is_error]
    valid_reads = [
        r for r in transcript.file_reads if r.has_result and not r.is_error and r.file_path
    ]
    valid_paginations = [p for p in transcript.paginations if p.has_result and not p.is_error]

    content_searches = [s for s in valid_searches if s.tool_name.lower() not in _LISTING_ONLY_TOOLS]

    findings: list[Finding] = []
    emitted_keys: set[tuple[str, str]] = set()

    def _emit(key_detail: str, finding: Finding) -> None:
        key = (finding.code, key_detail)
        if key not in emitted_keys:
            emitted_keys.add(key)
            findings.append(finding)

    for claim in scope_claims:
        combined_text = f"{claim.text} {claim.context}"
        claimed_scopes = extract_scope_qualifiers(claim.context, base_dirs=base_dirs)
        if claim.file_path:
            norm_fp = normalize_path(claim.file_path, base_dirs=base_dirs)
            if norm_fp and norm_fp not in (".", "./") and norm_fp not in claimed_scopes:
                claimed_scopes.insert(0, norm_fp)

        is_pagination_focused = bool(_PAGINATION_CLAIM_RE.search(combined_text)) and not bool(
            _FILE_OR_CODE_SEARCH_CLAIM_RE.search(combined_text)
        )

        # --- 1. Unread pagination cursor check ---
        if _pagination_applies_to_claim(
            claim, scope_receipt.unread_cursors, has_code_searches=bool(valid_searches)
        ):
            cur_key = ",".join(str(c.get("event_index", "")) for c in scope_receipt.unread_cursors)
            cursor_summaries: list[str] = []
            for cur in scope_receipt.unread_cursors:
                tool = str(cur.get("tool_name") or "tool")
                nc = cur.get("next_cursor")
                pg = cur.get("page")
                tp = cur.get("total_pages")
                if pg is not None and tp is not None:
                    cursor_summaries.append(
                        f"{tool} stopped at page {pg}/{tp}"
                        + (f" (next_cursor={nc!r})" if nc else "")
                    )
                elif nc:
                    cursor_summaries.append(f"{tool} has unread next_cursor={nc!r}")
                else:
                    cursor_summaries.append(f"{tool} returned has_more=true")
            _emit(
                cur_key,
                Finding(
                    severity="error",
                    code="UNREAD_PAGINATION_CURSOR",
                    claim=claim,
                    evidence={"unread_cursors": scope_receipt.unread_cursors},
                    message=(
                        f"Claimed `{claim.text}`, but paginated results were left unread: "
                        f"{'; '.join(cursor_summaries)}."
                    ),
                    suggestion=_format_cursor_suggestion(scope_receipt.unread_cursors),
                ),
            )
            # If the claim is purely about paginated API pages/results and there are no searches,
            # don't additionally spam NO_SEARCH_EVIDENCE.
            if is_pagination_focused and not valid_searches:
                continue

        # Purely pagination-focused claim where all cursors were followed: clean unless truncated
        if is_pagination_focused and valid_paginations and not valid_searches:
            # Still check if any pagination event was truncated
            trunc_pags = [p for p in valid_paginations if p.truncated]
            if trunc_pags:
                ev_key = ",".join(str(p.event_index) for p in trunc_pags)
                _emit(
                    ev_key,
                    Finding(
                        severity="error",
                        code="TRUNCATED_SEARCH_OUTPUT",
                        claim=claim,
                        evidence={
                            "truncated_events": [
                                {
                                    "event_index": p.event_index,
                                    "tool_name": p.tool_name,
                                    "reason": p.truncation_reason or "truncated",
                                }
                                for p in trunc_pags
                            ]
                        },
                        message=(
                            f"Claimed `{claim.text}`, but paginated tool output was truncated."
                        ),
                        suggestion="Fetch untruncated results before claiming full coverage.",
                    ),
                )
            continue

        # --- 2. File-scoped claim satisfied by a full FileReadEvent ---
        file_scoped_reads_cover = False
        if claimed_scopes and all(_looks_like_file_path(sc) for sc in claimed_scopes):
            matched_all = True
            truncated_file_reads = []
            for sc in claimed_scopes:
                f_reads = [
                    r for r in valid_reads if paths_match(sc, r.file_path, base_dirs=base_dirs)
                ]
                if not f_reads:
                    matched_all = False
                    break
                if all(r.truncated for r in f_reads):
                    truncated_file_reads.extend(f_reads)
            if matched_all:
                file_scoped_reads_cover = True
                if truncated_file_reads:
                    ev_key = ",".join(str(r.event_index) for r in truncated_file_reads)
                    _emit(
                        ev_key,
                        Finding(
                            severity="error",
                            code="TRUNCATED_SEARCH_OUTPUT",
                            claim=claim,
                            evidence={
                                "truncated_events": [
                                    {
                                        "event_index": r.event_index,
                                        "tool_name": r.tool_name,
                                        "file_path": r.file_path,
                                        "reason": r.truncation_reason or "truncated",
                                    }
                                    for r in truncated_file_reads
                                ]
                            },
                            message=(
                                f"Claimed `{claim.text}`, but the read of "
                                f"`{', '.join(claimed_scopes)}` was truncated."
                            ),
                            suggestion=f"Read all lines of {', '.join(claimed_scopes)} completely.",
                        ),
                    )
                continue

        # --- 3. No search evidence check ---
        if not valid_searches and not file_scoped_reads_cover:
            _emit(
                "no_search",
                Finding(
                    severity="error",
                    code="NO_SEARCH_EVIDENCE",
                    claim=claim,
                    evidence={
                        "searches": 0,
                        "files_read": len(scope_receipt.files_read),
                        "claimed_scopes": claimed_scopes,
                    },
                    message=(
                        f"Claimed `{claim.text}`, but no repository search was performed "
                        f"in the transcript."
                    ),
                    suggestion=(
                        "Run a repository-wide search (e.g., Grep or rg from the repo root) "
                        "before claiming absence or completeness."
                    ),
                ),
            )
            continue

        # For content-absence claims (callers/references/matches), prefer content searches
        # over bare directory listings (Glob/LS) when content searches exist.
        prefers_content_search = bool(_FILE_OR_CODE_SEARCH_CLAIM_RE.search(combined_text)) and not (
            re.search(
                r"\bevery\s+(?:single\s+)?file\b|\ball\s+files\b", combined_text, re.IGNORECASE
            )
        )
        relevant_searches = (
            content_searches if (prefers_content_search and content_searches) else valid_searches
        )

        # --- 4. Truncated search / output check ---
        unresolved_truncated = [
            s
            for s in relevant_searches
            if s.truncated
            and not _truncation_resolved_by_later_search(s, valid_searches, base_dirs)
        ]
        # If claimed_scopes is set, filter truncated searches to ones overlapping the claimed scopes
        if claimed_scopes and unresolved_truncated:
            scoped_trunc = [
                s
                for s in unresolved_truncated
                if any(
                    path_is_within(cs, sr, base_dirs=base_dirs)
                    or path_is_within(sr, cs, base_dirs=base_dirs)
                    for cs in claimed_scopes
                    for sr in _search_roots_for_event(s)
                )
            ]
            unresolved_truncated = scoped_trunc

        if unresolved_truncated:
            trunc_details = [
                {
                    "event_index": s.event_index,
                    "tool_name": s.tool_name,
                    "query": s.query,
                    "reason": s.truncation_reason or "truncated",
                }
                for s in unresolved_truncated
            ]
            ev_key = ",".join(str(s.event_index) for s in unresolved_truncated)
            reasons = ", ".join(
                f"{s.tool_name}#{s.event_index} ({s.truncation_reason or 'truncated'})"
                for s in unresolved_truncated
            )
            _emit(
                ev_key,
                Finding(
                    severity="error",
                    code="TRUNCATED_SEARCH_OUTPUT",
                    claim=claim,
                    evidence={"truncated_searches": trunc_details},
                    message=(
                        f"Claimed `{claim.text}`, but search output was truncated: {reasons}."
                    ),
                    suggestion=(
                        "Re-run the search without head_limit/truncation before claiming "
                        "absence or completeness."
                    ),
                ),
            )

        # --- 5. Narrow search scope check ---
        all_searched_roots: list[str] = []
        seen_roots: set[str] = set()
        for s in relevant_searches:
            for r in _search_roots_for_event(s):
                norm_r = normalize_path(r, base_dirs=base_dirs)
                if norm_r not in seen_roots:
                    seen_roots.add(norm_r)
                    all_searched_roots.append(norm_r)

        covers_repo_root = any(
            is_repo_root_path(r, base_dirs=base_dirs) for r in all_searched_roots
        )
        if not covers_repo_root:
            if claimed_scopes:
                uncovered = [
                    cs
                    for cs in claimed_scopes
                    if not any(
                        path_is_within(cs, sr, base_dirs=base_dirs) for sr in all_searched_roots
                    )
                ]
                if uncovered:
                    scope_key = ",".join(sorted(uncovered))
                    _emit(
                        scope_key,
                        Finding(
                            severity="error",
                            code="NARROW_SEARCH_SCOPE",
                            claim=claim,
                            evidence={
                                "claimed_scopes": claimed_scopes,
                                "uncovered_scopes": uncovered,
                                "searched_roots": all_searched_roots,
                            },
                            message=(
                                f"Claimed `{claim.text}` for `{', '.join(uncovered)}`, but "
                                f"searches only covered `{', '.join(all_searched_roots)}`."
                            ),
                            suggestion=(
                                f"Search `{', '.join(uncovered)}` (or the repository root '.') "
                                f"to verify this claim."
                            ),
                        ),
                    )
            else:
                scope_key = ",".join(sorted(all_searched_roots))
                _emit(
                    scope_key,
                    Finding(
                        severity="error",
                        code="NARROW_SEARCH_SCOPE",
                        claim=claim,
                        evidence={
                            "searched_roots": all_searched_roots,
                            "repo_root_searched": False,
                        },
                        message=(
                            f"Made repo-wide claim `{claim.text}`, but searches only targeted "
                            f"subdirectory/path `{', '.join(all_searched_roots)}`."
                        ),
                        suggestion=(
                            f"Search from the repository root ('.') instead of only "
                            f"`{', '.join(all_searched_roots)}`, or qualify the claim."
                        ),
                    ),
                )

        # --- 6. Non-trivial excluded directories check ---
        non_trivial_excluded: list[str] = []
        seen_excl: set[str] = set()
        for s in relevant_searches:
            for d in s.excluded_dirs:
                norm_d = _normalize_excluded_dir(d)
                if norm_d and not _is_trivial_excluded_dir(norm_d) and norm_d not in seen_excl:
                    seen_excl.add(norm_d)
                    non_trivial_excluded.append(norm_d)

        if non_trivial_excluded:
            # Skip if the agent explicitly acknowledged the exclusion in the claim sentence
            ctx_low = claim.context.lower()
            unacknowledged = [
                d
                for d in non_trivial_excluded
                if not (
                    _EXCLUSION_ACKNOWLEDGED_RE.search(ctx_low)
                    and (d.lower() in ctx_low or posixpath.basename(d).lower() in ctx_low)
                )
            ]
            if unacknowledged:
                excl_key = ",".join(sorted(unacknowledged))
                _emit(
                    excl_key,
                    Finding(
                        severity="warning",
                        code="EXCLUDED_SEARCH_DIRS",
                        claim=claim,
                        evidence={"excluded_dirs": unacknowledged},
                        message=(
                            f"Claimed `{claim.text}`, while search explicitly excluded "
                            f"non-trivial directory/pattern(s): `{', '.join(unacknowledged)}`."
                        ),
                        suggestion=(
                            f"Re-run search without excluding `{', '.join(unacknowledged)}` or "
                            f"explicitly qualify the claim."
                        ),
                    ),
                )

    return findings

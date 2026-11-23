"""Absence and completeness claim verifier against transcript search & pagination receipts."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from handoff_verify.models import (
    Claim,
    CommandEvent,
    FileReadEvent,
    Finding,
    ParsedTranscript,
    ScopeReceipt,
    SearchEvent,
)
from handoff_verify.parsers._common import split_lf_lines
from handoff_verify.verifiers._paths import (
    build_base_dirs,
    is_repo_root_path,
    normalize_path,
    path_is_within,
    paths_match,
    resolve_in_repo,
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

_STRONG_PAGINATION_RE = re.compile(
    r"\b(?:pages?|pagination|cursors?|issues?|prs?|pull\s+requests?|tickets?|"
    r"records?|entries|comments?|commits?|branches?)\b",
    re.IGNORECASE,
)

_GENERIC_PAGINATION_RE = re.compile(
    r"\b(?:results?|items?|all\s+\d+)\b",
    re.IGNORECASE,
)

_FILE_OR_CODE_SEARCH_CLAIM_RE = re.compile(
    r"\b(?:callers?|references?|usages?|uses|occurrences?|importers?|consumers?|"
    r"implementations?|definitions?|codebase|repo|repository|files?|todos?|fixmes?|"
    r"anywhere|nowhere\s+else|matches|grep(?:ped)?|rg)\b",
    re.IGNORECASE,
)

_ISSUE_OR_PR_CLAIM_RE = re.compile(
    r"\b(?:issues?|prs?|pull\s+requests?|tickets?|items?|pages?)\b",
    re.IGNORECASE,
)

_LISTING_CMD_RE = re.compile(
    r"\b(?:gh\s+(?:issue|pr|release|run|workflow)\s+list|glab\s+(?:issue|mr)\s+list|"
    r"jira\s+|linear\s+|curl\s+)\b",
    re.IGNORECASE,
)

_EXCLUSION_ACKNOWLEDGED_RE = re.compile(
    r"\b(?:exclud(?:ing|ed)|outside(?:\s+of)?|other\s+than|apart\s+from|ignoring|except)\b",
    re.IGNORECASE,
)

_BACKTICK_TERM_RE = re.compile(r"`([^`\n]{2,120})`")


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


def _extract_claim_query_terms(
    claim: Claim,
    claimed_scopes: list[str],
    base_dirs: tuple[str, ...],
    excluded_dirs: set[str] | None = None,
) -> list[str]:
    """Extract backticked symbol/query terms from a claim (excluding scope/excluded paths)."""
    scope_set = {normalize_path(s, base_dirs=base_dirs).lower() for s in claimed_scopes}
    excl_set = {d.lower() for d in (excluded_dirs or set())}
    ctx = claim.context or ""
    terms: list[str] = []
    seen: set[str] = set()
    for m in _BACKTICK_TERM_RE.finditer(ctx):
        raw = m.group(1).strip()
        if not raw or " " in raw or "/" in raw:
            continue
        # Skip terms immediately preceded by exclusion/location prepositions ("outside `legacy`")
        pre = ctx[max(0, m.start() - 25) : m.start()]
        if re.search(
            r"\b(?:outside(?:\s+of)?|excluding|except|apart\s+from|other\s+than|"
            r"in|under|within|inside)\s+$",
            pre,
            re.IGNORECASE,
        ):
            continue
        norm = normalize_path(raw, base_dirs=base_dirs).lower()
        if norm in scope_set or norm in excl_set:
            continue
        low = raw.lower()
        if low not in seen:
            seen.add(low)
            terms.append(raw)
    return terms


def _is_listing_only_search(search: SearchEvent) -> bool:
    """Return True if a SearchEvent only listed filenames (Glob/LS/find/fd without content grep)."""
    tool_low = search.tool_name.lower()
    base_tool = tool_low.rsplit(":", 1)[-1]
    if base_tool not in _LISTING_ONLY_TOOLS:
        return False
    # R2-8 caveat: `find ... | xargs grep` / `find ... -exec grep` IS a content search
    raw_cmd = ""
    if isinstance(search.raw_input, dict):
        cmd_val = search.raw_input.get("command") or search.raw_input.get("cmd")
        if isinstance(cmd_val, str):
            raw_cmd = cmd_val
    return not (raw_cmd and re.search(r"\b(?:grep|rg|ripgrep|ag|ack|git\s+grep)\b", raw_cmd))


def _search_matches_terms(search: SearchEvent, terms: list[str]) -> bool:
    """Return True if a SearchEvent's query relates to any extracted claim symbol term."""
    if not search.query or not terms:
        return False
    q_low = search.query.lower()
    for term in terms:
        t_low = term.lower()
        if t_low in q_low or (len(q_low) >= 3 and q_low in t_low):
            return True
        # Also support regex queries like `legacy_(auth|login)` matching `legacy_auth`
        try:
            if re.search(search.query, term, re.IGNORECASE):
                return True
        except re.error:
            pass
    return False


def _is_partial_file_read(
    f_reads: list[FileReadEvent],
    scope: str,
    repo_root: Path | None,
    base_dirs: tuple[str, ...],
) -> bool:
    """Return True if reads for `scope` are truncated or windowed (did not cover the full file)."""
    if all(r.truncated for r in f_reads):
        return True

    # Check if any read was an untruncated full-file read (no offset > 1 and no cap hit)
    windows: list[tuple[int, int]] = []
    has_unbounded_full_read = False
    for r in f_reads:
        if r.truncated:
            continue
        start = r.start_line if r.start_line is not None else (r.offset or 1)
        end = r.end_line
        if end is None and r.line_count > 0:
            end = start + r.line_count - 1
        if start >= 1 and end is not None and end >= start:
            windows.append((start, end))

        hit_limit = r.limit is not None and r.line_count >= r.limit
        if start <= 1 and not hit_limit:
            has_unbounded_full_read = True

    if has_unbounded_full_read:
        return False

    # If repo_root is available, check if merged windows actually cover the entire live file!
    if repo_root is not None and windows:
        resolved = resolve_in_repo(scope, repo_root, base_dirs=base_dirs)
        if resolved is not None and resolved.is_file():
            try:
                total = len(split_lf_lines(resolved.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                total = None
            if total is not None:
                windows.sort()
                merged = [windows[0]]
                for s, e in windows[1:]:
                    ps, pe = merged[-1]
                    if s <= pe + 1:
                        merged[-1] = (ps, max(pe, e))
                    else:
                        merged.append((s, e))
                if any(ws <= 1 and we >= total for ws, we in merged):
                    return False

    return True


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
        if trunc_search.query and later.query != trunc_search.query:
            continue
        later_roots = _search_roots_for_event(later)
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

    # Check if the claim explicitly names an unread pagination resource (e.g. `issues`)
    names_resource = any(
        (res := str(cur.get("resource") or "").lower()) and len(res) >= 3 and res in combined
        for cur in unread_cursors
    )
    has_strong_pag = bool(_STRONG_PAGINATION_RE.search(combined))
    has_generic_pag = bool(_GENERIC_PAGINATION_RE.search(combined))
    has_code_search_terms = bool(_FILE_OR_CODE_SEARCH_CLAIM_RE.search(combined))

    if claim.file_path and not (has_strong_pag or names_resource):
        return False

    # B10: Do NOT apply unrelated API pagination cursors to explicit code-grep claims
    # ("Grep for `legacy_auth` returned no results anywhere in the repo")!
    if has_code_search_terms and not (has_strong_pag or names_resource):
        return False

    if claim.kind == "completeness" and (
        has_strong_pag or has_generic_pag or names_resource or not has_code_searches
    ):
        return True
    if claim.kind == "absence":
        if has_strong_pag or names_resource:
            return True
        if has_generic_pag and not has_code_searches:
            return True
        if not has_code_searches:
            return True
    return False


def _has_listing_command_evidence(commands: list[CommandEvent]) -> bool:
    """Return True if a completed non-errored CLI listing command (e.g. `gh issue list`) ran."""
    return any(
        cmd.has_result and not cmd.is_error and cmd.command and _LISTING_CMD_RE.search(cmd.command)
        for cmd in commands
    )


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

    from handoff_verify.extractors import (
        extract_scope_qualifiers,
        has_explicit_repo_wide_wording,
    )

    base_dirs = build_base_dirs(transcript.cwd, repo_root)
    valid_searches = [s for s in transcript.searches if s.has_result and not s.is_error]
    valid_reads = [
        r for r in transcript.file_reads if r.has_result and not r.is_error and r.file_path
    ]
    valid_paginations = [p for p in transcript.paginations if p.has_result and not p.is_error]

    content_searches = [s for s in valid_searches if not _is_listing_only_search(s)]
    has_listing_cmd = _has_listing_command_evidence(transcript.commands)

    findings: list[Finding] = []
    emitted_keys: set[tuple[str, str]] = set()

    def _emit(key_detail: str, finding: Finding) -> None:
        key = (finding.code, key_detail)
        if key not in emitted_keys:
            emitted_keys.add(key)
            findings.append(finding)

    for claim in scope_claims:
        combined_text = f"{claim.text} {claim.context}"
        repo_wide_explicit = has_explicit_repo_wide_wording(claim.context or "")
        if repo_wide_explicit:
            claimed_scopes: list[str] = []
        else:
            claimed_scopes = extract_scope_qualifiers(claim.context, base_dirs=base_dirs)
            if claim.file_path:
                norm_fp = normalize_path(claim.file_path, base_dirs=base_dirs)
                if norm_fp and norm_fp not in (".", "./") and norm_fp not in claimed_scopes:
                    claimed_scopes.insert(0, norm_fp)

        is_pagination_focused = bool(_STRONG_PAGINATION_RE.search(combined_text)) and not bool(
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
            if is_pagination_focused and not valid_searches:
                continue

        # Purely pagination/issue-listing claim satisfied by followed paginations or listing CLI
        if is_pagination_focused and not valid_searches and (valid_paginations or has_listing_cmd):
            trunc_pags = [p for p in valid_paginations if p.truncated]
            trunc_cmds = [
                c
                for c in transcript.commands
                if c.has_result
                and not c.is_error
                and c.truncated
                and _LISTING_CMD_RE.search(c.command or "")
            ]
            if trunc_pags or trunc_cmds:
                trunc_evs = [
                    {
                        "event_index": ev.event_index,
                        "tool_name": ev.tool_name,
                        "reason": ev.truncation_reason or "truncated",
                    }
                    for ev in list(trunc_pags) + list(trunc_cmds)
                ]
                ev_key = ",".join(str(item["event_index"]) for item in trunc_evs)
                _emit(
                    ev_key,
                    Finding(
                        severity="error",
                        code="TRUNCATED_SEARCH_OUTPUT",
                        claim=claim,
                        evidence={"truncated_events": trunc_evs},
                        message=(
                            f"Claimed `{claim.text}`, but listing/paginated output was truncated."
                        ),
                        suggestion="Fetch untruncated results before claiming full coverage.",
                    ),
                )
            continue

        # --- 2. File-scoped claim satisfied by a full FileReadEvent (B11: rejects windowed) ---
        if claimed_scopes and all(_looks_like_file_path(sc) for sc in claimed_scopes):
            matched_all = True
            incomplete_file_reads: list[FileReadEvent] = []
            for sc in claimed_scopes:
                f_reads = [
                    r for r in valid_reads if paths_match(sc, r.file_path, base_dirs=base_dirs)
                ]
                if not f_reads:
                    matched_all = False
                    break
                # Check if a content search covered this file as an alternative
                searched_file = any(
                    not s.truncated
                    and any(
                        path_is_within(sc, sr, base_dirs=base_dirs)
                        for sr in _search_roots_for_event(s)
                    )
                    for s in content_searches
                )
                if not searched_file and _is_partial_file_read(f_reads, sc, repo_root, base_dirs):
                    incomplete_file_reads.extend(f_reads)
            if matched_all:
                if incomplete_file_reads:
                    ev_key = ",".join(str(r.event_index) for r in incomplete_file_reads)
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
                                        "reason": r.truncation_reason or "partial/windowed read",
                                    }
                                    for r in incomplete_file_reads
                                ]
                            },
                            message=(
                                f"Claimed `{claim.text}`, but only a partial/windowed read of "
                                f"`{', '.join(claimed_scopes)}` was performed."
                            ),
                            suggestion=f"Read all lines of {', '.join(claimed_scopes)} completely.",
                        ),
                    )
                continue

        # Determine whether this claim requires content search (vs file-listing Glob/LS)
        prefers_content_search = bool(_FILE_OR_CODE_SEARCH_CLAIM_RE.search(combined_text)) and not (
            re.search(
                r"\bevery\s+(?:single\s+)?file\b|\ball\s+files\b|\bno\s+other\s+files\b",
                combined_text,
                re.IGNORECASE,
            )
        )

        # --- 3. No search evidence check (B12: listing-only Glob cannot satisfy content claim) ---
        if not valid_searches or (prefers_content_search and not content_searches):
            if _ISSUE_OR_PR_CLAIM_RE.search(combined_text) and has_listing_cmd:
                continue
            _emit(
                "no_search",
                Finding(
                    severity="error",
                    code="NO_SEARCH_EVIDENCE",
                    claim=claim,
                    evidence={
                        "searches": len(valid_searches),
                        "content_searches": len(content_searches),
                        "files_read": len(scope_receipt.files_read),
                        "claimed_scopes": claimed_scopes,
                    },
                    message=(
                        f"Claimed `{claim.text}`, but no content search (Grep/rg) was performed "
                        f"in the transcript."
                        if valid_searches
                        else f"Claimed `{claim.text}`, but no repository search was performed "
                        f"in the transcript."
                    ),
                    suggestion=(
                        "Run a repository-wide content search (Grep/rg from the repo root) "
                        "before claiming absence or completeness."
                    ),
                ),
            )
            continue

        relevant_searches = content_searches if prefers_content_search else valid_searches

        # B10 + B12 + R2-8: Tie evidence to the claimed symbol/query term when present!
        all_excl_names = {
            _normalize_excluded_dir(d) for s in relevant_searches for d in s.excluded_dirs
        }
        claim_terms = _extract_claim_query_terms(
            claim, claimed_scopes, base_dirs, excluded_dirs=all_excl_names
        )
        if claim_terms:
            query_matched = [s for s in relevant_searches if _search_matches_terms(s, claim_terms)]
            if query_matched:
                relevant_searches = query_matched
            elif prefers_content_search and all(bool(s.query) for s in relevant_searches):
                # R2-8: An unrelated root Grep (e.g. `import os`) cannot verify
                # "no callers of `legacy_auth` anywhere in the repo".
                _emit(
                    "no_matching_query:" + ",".join(sorted(t.lower() for t in claim_terms)),
                    Finding(
                        severity="error",
                        code="NO_SEARCH_EVIDENCE",
                        claim=claim,
                        evidence={
                            "claimed_terms": claim_terms,
                            "observed_queries": [s.query for s in relevant_searches],
                        },
                        message=(
                            f"Claimed `{claim.text}` for `{', '.join(claim_terms)}`, but no "
                            f"search in the transcript queried that term."
                        ),
                        suggestion=(
                            f"Run a repository-wide search for `{claim_terms[0]}` before "
                            f"claiming absence."
                        ),
                    ),
                )
                continue

        # --- 4. Truncated search / output check ---
        unresolved_truncated = [
            s
            for s in relevant_searches
            if s.truncated
            and not _truncation_resolved_by_later_search(s, relevant_searches, base_dirs)
        ]
        if claimed_scopes and unresolved_truncated:
            unresolved_truncated = [
                s
                for s in unresolved_truncated
                if any(
                    path_is_within(cs, sr, base_dirs=base_dirs)
                    or path_is_within(sr, cs, base_dirs=base_dirs)
                    for cs in claimed_scopes
                    for sr in _search_roots_for_event(s)
                )
            ]

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

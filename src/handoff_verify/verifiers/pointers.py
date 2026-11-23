"""File pointer claim verifier (read coverage, line windows, staleness, live repo)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from handoff_verify.models import (
    Claim,
    CommandEvent,
    FileEditEvent,
    FileReadEvent,
    Finding,
    ParsedTranscript,
    SearchEvent,
)
from handoff_verify.parsers._common import split_lf_lines
from handoff_verify.verifiers._paths import (
    build_base_dirs,
    is_exact_path_match,
    normalize_path,
    paths_match,
    resolve_in_repo,
)

_GREP_CONTENT_LINE_RE = re.compile(r"^([^\s:][^:\n]*):(\d+)[:-]")
_EDIT_SUMMARY_VERB_RE = re.compile(
    r"\b(?:updated?|edited?|modif(?:y|ied)|fixed?|changed?|patched?|"
    r"added?|removed?|deleted?|refactored?|wrote|written|created?|applied?|replaced?)\b",
    re.IGNORECASE,
)


def _merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or directly adjacent (start, end) 1-based inclusive line windows."""
    valid = sorted((s, e) for s, e in windows if s >= 1 and e >= s)
    if not valid:
        return []
    merged: list[tuple[int, int]] = [valid[0]]
    for s, e in valid[1:]:
        prev_s, prev_e = merged[-1]
        if s <= prev_e + 1:
            merged[-1] = (prev_s, max(prev_e, e))
        else:
            merged.append((s, e))
    return merged


def _window_covers(merged: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(ws <= start and end <= we for ws, we in merged)


def _format_uncovered_suggestion(
    file_path: str,
    c_start: int,
    c_end: int,
    merged: list[tuple[int, int]],
) -> str:
    """Build an actionable suggestion naming the exact unread line range."""
    if not merged:
        if c_start == c_end:
            return f"Read line {c_start} of {file_path}"
        return f"Read lines {c_start}-{c_end} of {file_path}"

    max_end = merged[-1][1]
    min_start = merged[0][0]
    if c_start > max_end:
        return f"Read lines {max_end + 1}-{c_end} of {file_path}"
    if c_end < min_start:
        return f"Read lines {c_start}-{min_start - 1} of {file_path}"

    # Compute uncovered sub-intervals inside [c_start, c_end]
    uncovered: list[tuple[int, int]] = []
    cur = c_start
    for ws, we in merged:
        if we < cur:
            continue
        if ws > c_end:
            break
        if ws > cur:
            uncovered.append((cur, min(ws - 1, c_end)))
        cur = max(cur, we + 1)
    if cur <= c_end:
        uncovered.append((cur, c_end))

    if uncovered:
        parts = [f"{s}-{e}" if s != e else str(s) for s, e in uncovered]
        return f"Read lines {', '.join(parts)} of {file_path}"
    return f"Read lines {c_start}-{c_end} of {file_path}"


def _read_event_window(ev: FileReadEvent) -> tuple[int, int] | None:
    if ev.start_line is not None and ev.end_line is not None and ev.end_line >= ev.start_line >= 1:
        return (ev.start_line, ev.end_line)
    if ev.offset is not None and ev.limit is not None and ev.offset >= 1 and ev.limit >= 1:
        return (ev.offset, ev.offset + ev.limit - 1)
    if ev.start_line is not None and ev.start_line >= 1 and ev.line_count > 0:
        return (ev.start_line, ev.start_line + ev.line_count - 1)
    return None


def _write_event_window(ev: FileEditEvent) -> tuple[int, int] | None:
    if ev.operation != "write" or ev.content is None:
        return None
    line_count = len(split_lf_lines(ev.content))
    return (1, max(1, line_count))


_T = TypeVar("_T")


def _filter_matching_events(
    items: list[_T],
    path_getter: Callable[[_T], str],
    claimed_path: str,
    base_dirs: tuple[str, ...],
) -> list[_T]:
    """Match events by exact normalized path first; fall back to basename only if no exact match."""
    exact = [
        item
        for item in items
        if path_getter(item)
        and is_exact_path_match(claimed_path, path_getter(item), base_dirs=base_dirs)
    ]
    if exact:
        return exact
    return [
        item
        for item in items
        if path_getter(item) and paths_match(claimed_path, path_getter(item), base_dirs=base_dirs)
    ]


def _grep_observed_lines(
    searches: list[SearchEvent],
    claimed_path: str,
    base_dirs: tuple[str, ...],
) -> set[int]:
    """Return 1-based line numbers explicitly shown in grep/rg content output for claimed_path."""
    observed: set[int] = set()
    for search in searches:
        if not search.has_result or search.is_error or not search.output:
            continue
        for line in split_lf_lines(search.output):
            m = _GREP_CONTENT_LINE_RE.match(line)
            if not m:
                continue
            path_part = m.group(1)
            if paths_match(claimed_path, path_part, base_dirs=base_dirs):
                try:
                    line_num = int(m.group(2))
                except ValueError:
                    continue
                if line_num >= 1:
                    observed.add(line_num)
    return observed


def _file_mentioned_in_searches(
    searches: list[SearchEvent],
    claimed_path: str,
    base_dirs: tuple[str, ...],
) -> bool:
    for search in searches:
        if not search.has_result or search.is_error:
            continue
        for mf in search.matched_files:
            if paths_match(claimed_path, mf, base_dirs=base_dirs):
                return True
        for sp in search.search_paths:
            if paths_match(claimed_path, sp, base_dirs=base_dirs):
                return True
        if search.search_path and paths_match(
            claimed_path, search.search_path, base_dirs=base_dirs
        ):
            return True
    return False


def _file_mentioned_in_commands(
    commands: list[CommandEvent],
    claimed_path: str,
    base_dirs: tuple[str, ...],
) -> bool:
    """Return True if a non-errored command stdout explicitly mentions the normalized file path."""
    norm_c = normalize_path(claimed_path, base_dirs=base_dirs)
    if not norm_c:
        return False
    # Require boundary around path so `app.py` doesn't match `myapp.py`
    pattern = re.compile(r"(?:^|[\s`'\"(/:])" + re.escape(norm_c) + r"(?:$|[\s`'\"):,;\r\n])")
    for cmd in commands:
        if not cmd.has_result or cmd.is_error:
            continue
        if cmd.stdout and pattern.search(cmd.stdout):
            return True
    return False


def _claim_refers_to_in_place_edit(
    claim: Claim,
    edit: FileEditEvent,
    last_read: FileReadEvent | None,
) -> bool:
    """Return True if a line-specific claim refers to an exact same-line-count edit just made."""
    if edit.operation != "edit":
        return False
    if edit.old_text is None or edit.new_text is None:
        return False
    old_lines = split_lf_lines(edit.old_text)
    new_lines = split_lf_lines(edit.new_text)
    if len(old_lines) != len(new_lines):
        # Line count changed, so line numbers in the file may have shifted!
        return False

    ctx = claim.context or ""
    has_edit_verb = bool(_EDIT_SUMMARY_VERB_RE.search(ctx))
    # Check if the sentence mentions a non-trivial line/snippet from new_text
    quotes_new = any(len(ln.strip()) >= 6 and ln.strip() in ctx for ln in new_lines if ln.strip())
    if not (has_edit_verb or quotes_new):
        return False

    # Verify the claimed line actually matches where old_text was located in last_read (if known)
    if last_read is not None and last_read.content and last_read.start_line is not None:
        read_lines = split_lf_lines(last_read.content)
        c_start = claim.start_line
        if c_start is not None and last_read.start_line <= c_start <= (last_read.end_line or 0):
            rel_idx = c_start - last_read.start_line
            if 0 <= rel_idx < len(read_lines):
                line_text = read_lines[rel_idx].strip()
                if any(
                    ol.strip() and (ol.strip() in line_text or line_text in ol.strip())
                    for ol in old_lines
                ):
                    return True
            return False

    return quotes_new


def verify_pointers(
    claims: list[Claim],
    transcript: ParsedTranscript,
    *,
    repo_root: Path | None = None,
) -> list[Finding]:
    """Verify file pointer claims against transcript reads, edits, searches, and optional repo."""
    base_dirs = build_base_dirs(transcript.cwd, repo_root)
    valid_reads = [
        r for r in transcript.file_reads if r.has_result and not r.is_error and r.file_path
    ]
    valid_edits = [
        e for e in transcript.file_edits if e.has_result and not e.is_error and e.file_path
    ]
    valid_searches = [s for s in transcript.searches if s.has_result and not s.is_error]

    findings: list[Finding] = []
    live_file_cache: dict[Path, list[str] | None] = {}

    def _read_live_lines(path: Path) -> list[str] | None:
        if path not in live_file_cache:
            if not path.is_file():
                live_file_cache[path] = None
            else:
                try:
                    raw = path.read_text(encoding="utf-8", errors="replace")
                    live_file_cache[path] = split_lf_lines(raw)
                except OSError:
                    live_file_cache[path] = None
        return live_file_cache[path]

    for claim in claims:
        if claim.kind != "pointer" or not claim.file_path:
            continue

        claimed_path = claim.file_path
        norm_path = normalize_path(claimed_path, base_dirs=base_dirs)
        c_start = claim.start_line
        c_end = claim.end_line if claim.end_line is not None else c_start

        reads = sorted(
            _filter_matching_events(valid_reads, lambda r: r.file_path, claimed_path, base_dirs),
            key=lambda e: e.event_index,
        )
        edits = sorted(
            _filter_matching_events(valid_edits, lambda e: e.file_path, claimed_path, base_dirs),
            key=lambda e: e.event_index,
        )
        grep_lines = (
            _grep_observed_lines(valid_searches, claimed_path, base_dirs) if c_start else set()
        )
        in_searches = _file_mentioned_in_searches(valid_searches, claimed_path, base_dirs)

        write_edits = [e for e in edits if e.operation == "write"]
        unread_fired = False

        # --- 1. Unread file check ---
        if c_start is not None:
            assert c_end is not None
            grep_covers_range = bool(grep_lines) and all(
                ln in grep_lines for ln in range(c_start, c_end + 1)
            )
            if not reads and not write_edits and not grep_covers_range:
                unread_fired = True
                if in_searches:
                    reason = "only listed/matched in search output without reading file contents"
                elif edits:
                    reason = "edited without reading file contents or line numbers"
                else:
                    reason = "never read in transcript"
                line_label = f"line {c_start}" if c_start == c_end else f"lines {c_start}-{c_end}"
                findings.append(
                    Finding(
                        severity="error",
                        code="UNREAD_FILE",
                        claim=claim,
                        evidence={
                            "file_path": norm_path,
                            "claimed_range": [c_start, c_end],
                            "observed_in_search": in_searches,
                            "reason": reason,
                        },
                        message=(f"Cited `{norm_path}` ({line_label}), but the file was {reason}."),
                        suggestion=f"Read {line_label} of {norm_path}",
                    )
                )
        else:
            in_commands = (
                False
                if (reads or edits or in_searches)
                else _file_mentioned_in_commands(transcript.commands, claimed_path, base_dirs)
            )
            if not reads and not edits and not in_searches and not in_commands:
                unread_fired = True
                findings.append(
                    Finding(
                        severity="error",
                        code="UNREAD_FILE",
                        claim=claim,
                        evidence={
                            "file_path": norm_path,
                            "observed_in_search": False,
                            "reason": "never read or observed in transcript",
                        },
                        message=(
                            f"Referenced `{norm_path}`, but it was never read, edited, or observed "
                            f"in the transcript."
                        ),
                        suggestion=f"Read {norm_path} before referencing it in your summary.",
                    )
                )

        # --- 2. Stale read & Line window checks (only if file was observed) ---
        stale_fired = False
        if not unread_fired:
            last_read = reads[-1] if reads else None
            last_edit = edits[-1] if edits else None

            # Check if file was edited after the last read
            if last_edit is not None and (
                last_read is None or last_edit.event_index > last_read.event_index
            ):
                if last_edit.operation == "write":
                    # Full file write: agent knows current content and line numbers
                    pass
                elif last_edit.operation == "delete":
                    stale_fired = True
                    findings.append(
                        Finding(
                            severity="error",
                            code="STALE_FILE_READ",
                            claim=claim,
                            evidence={
                                "file_path": norm_path,
                                "last_read_event_index": last_read.event_index
                                if last_read
                                else None,
                                "last_edit_event_index": last_edit.event_index,
                                "edit_operation": "delete",
                            },
                            message=(
                                f"Cited `{norm_path}`, which was deleted at event "
                                f"#{last_edit.event_index}."
                            ),
                            suggestion=f"Verify whether {norm_path} still exists before citing it.",
                        )
                    )
                else:
                    # `edit` or `bash_modify` after last read without re-reading
                    if c_start is not None:
                        if not _claim_refers_to_in_place_edit(claim, last_edit, last_read):
                            stale_fired = True
                            line_label = (
                                f"line {c_start}"
                                if c_start == c_end
                                else f"lines {c_start}-{c_end}"
                            )
                            findings.append(
                                Finding(
                                    severity="error",
                                    code="STALE_FILE_READ",
                                    claim=claim,
                                    evidence={
                                        "file_path": norm_path,
                                        "claimed_range": [c_start, c_end],
                                        "last_read_event_index": last_read.event_index
                                        if last_read
                                        else None,
                                        "last_edit_event_index": last_edit.event_index,
                                        "edit_operation": last_edit.operation,
                                    },
                                    message=(
                                        f"Cited `{norm_path}` ({line_label}) using a stale read: "
                                        f"the file was modified at event #{last_edit.event_index} "
                                        f"({last_edit.operation}) and not re-read afterward."
                                    ),
                                    suggestion=(
                                        f"Re-read {norm_path} after event #{last_edit.event_index} "
                                        f"to verify current line numbers."
                                    ),
                                )
                            )
                    else:
                        # File-level mention after edit: clean if summarizing the edit itself
                        if not _EDIT_SUMMARY_VERB_RE.search(claim.context or ""):
                            stale_fired = True
                            findings.append(
                                Finding(
                                    severity="warning",
                                    code="STALE_FILE_READ",
                                    claim=claim,
                                    evidence={
                                        "file_path": norm_path,
                                        "last_read_event_index": last_read.event_index
                                        if last_read
                                        else None,
                                        "last_edit_event_index": last_edit.event_index,
                                        "edit_operation": last_edit.operation,
                                    },
                                    message=(
                                        f"Referenced `{norm_path}` after modifying it at event "
                                        f"#{last_edit.event_index} ({last_edit.operation}) without "
                                        f"re-reading."
                                    ),
                                    suggestion=(
                                        f"Re-read {norm_path} to confirm its post-edit state."
                                    ),
                                )
                            )

            # Check line window coverage when a line range is cited
            if c_start is not None:
                assert c_end is not None
                # Determine which reads/writes apply to the current file version
                last_modifying_edit = edits[-1] if edits else None
                post_edit_reads = (
                    [r for r in reads if r.event_index > last_modifying_edit.event_index]
                    if last_modifying_edit is not None
                    else reads
                )
                windows: list[tuple[int, int]] = []
                unknown_untruncated_read = False

                if last_modifying_edit is not None and post_edit_reads:
                    # File WAS re-read after the last edit: check post-edit windows
                    for r in post_edit_reads:
                        w = _read_event_window(r)
                        if w is not None:
                            windows.append(w)
                        elif not r.truncated:
                            unknown_untruncated_read = True
                    if last_modifying_edit.operation == "write":
                        ww = _write_event_window(last_modifying_edit)
                        if ww is not None:
                            windows.append(ww)
                    merged_post = _merge_windows(windows)
                    if not unknown_untruncated_read and not _window_covers(
                        merged_post, c_start, c_end
                    ):
                        # Was it covered by pre-edit reads? If so, that's a stale read!
                        pre_windows = [w for r in reads if (w := _read_event_window(r)) is not None]
                        merged_pre = _merge_windows(pre_windows)
                        if _window_covers(merged_pre, c_start, c_end) and not stale_fired:
                            stale_fired = True
                            findings.append(
                                Finding(
                                    severity="error",
                                    code="STALE_FILE_READ",
                                    claim=claim,
                                    evidence={
                                        "file_path": norm_path,
                                        "claimed_range": [c_start, c_end],
                                        "last_edit_event_index": last_modifying_edit.event_index,
                                        "post_edit_windows": [[s, e] for s, e in merged_post],
                                    },
                                    message=(
                                        f"Cited `{norm_path}` lines {c_start}-{c_end}, which were "
                                        f"only read before event "
                                        f"#{last_modifying_edit.event_index} "
                                        f"and not re-read afterward."
                                    ),
                                    suggestion=_format_uncovered_suggestion(
                                        norm_path, c_start, c_end, merged_post
                                    ),
                                )
                            )
                        else:
                            win_post = ", ".join(f"{s}-{e}" for s, e in merged_post) or "none"
                            findings.append(
                                Finding(
                                    severity="error",
                                    code="LINE_OUT_OF_READ_WINDOW",
                                    claim=claim,
                                    evidence={
                                        "file_path": norm_path,
                                        "claimed_range": [c_start, c_end],
                                        "observed_windows": [[s, e] for s, e in merged_post],
                                    },
                                    message=(
                                        f"Cited `{norm_path}` ({c_start}-{c_end}), outside "
                                        f"observed read window(s) {win_post}."
                                    ),
                                    suggestion=_format_uncovered_suggestion(
                                        norm_path, c_start, c_end, merged_post
                                    ),
                                )
                            )
                else:
                    # No post-edit re-read (no edits, write edit, or stale read already flagged)
                    if last_modifying_edit is not None and last_modifying_edit.operation == "write":
                        ww = _write_event_window(last_modifying_edit)
                        if ww is not None:
                            windows.append(ww)
                    else:
                        for r in reads:
                            w = _read_event_window(r)
                            if w is not None:
                                windows.append(w)
                            elif not r.truncated:
                                unknown_untruncated_read = True
                        for we in write_edits:
                            ww = _write_event_window(we)
                            if ww is not None:
                                windows.append(ww)
                    for ln in grep_lines:
                        windows.append((ln, ln))

                    merged = _merge_windows(windows)
                    if (
                        not unknown_untruncated_read
                        and merged
                        and not _window_covers(merged, c_start, c_end)
                    ):
                        line_label = (
                            f"line {c_start}" if c_start == c_end else f"lines {c_start}-{c_end}"
                        )
                        win_str = ", ".join(f"{s}-{e}" for s, e in merged)
                        findings.append(
                            Finding(
                                severity="error",
                                code="LINE_OUT_OF_READ_WINDOW",
                                claim=claim,
                                evidence={
                                    "file_path": norm_path,
                                    "claimed_range": [c_start, c_end],
                                    "observed_windows": [[s, e] for s, e in merged],
                                },
                                message=(
                                    f"Cited `{norm_path}` ({line_label}), outside observed "
                                    f"read window(s) {win_str}."
                                ),
                                suggestion=_format_uncovered_suggestion(
                                    norm_path, c_start, c_end, merged
                                ),
                            )
                        )

        # --- 3. Live repo check (when repo_root is provided) ---
        if repo_root is not None:
            observed_paths = [r.file_path for r in reads] + [e.file_path for e in edits]
            resolved = resolve_in_repo(
                claimed_path,
                repo_root,
                base_dirs=base_dirs,
                fallback_observed=observed_paths,
            )
            live_lines = _read_live_lines(resolved)
            if live_lines is None:
                findings.append(
                    Finding(
                        severity="error",
                        code="FILE_NOT_FOUND",
                        claim=claim,
                        evidence={
                            "file_path": norm_path,
                            "resolved_path": str(resolved),
                        },
                        message=(
                            f"Cited file `{norm_path}` does not exist on disk in `{repo_root}`."
                        ),
                        suggestion=f"Check the exact path for `{norm_path}` in the repository.",
                    )
                )
            else:
                total_lines = len(live_lines)
                if c_start is not None:
                    assert c_end is not None
                    if c_start > total_lines or c_end > total_lines:
                        line_label = (
                            f"line {c_start}" if c_start == c_end else f"lines {c_start}-{c_end}"
                        )
                        findings.append(
                            Finding(
                                severity="error",
                                code="LINE_OUT_OF_BOUNDS",
                                claim=claim,
                                evidence={
                                    "file_path": norm_path,
                                    "claimed_range": [c_start, c_end],
                                    "live_line_count": total_lines,
                                },
                                message=(
                                    f"Cited `{norm_path}` ({line_label}), but the live file only "
                                    f"has {total_lines} line(s)."
                                ),
                                suggestion=(
                                    f"Re-read {norm_path} (1-{total_lines}) to check "
                                    f"valid line numbers."
                                ),
                            )
                        )
                    elif reads and not edits and not stale_fired:
                        # Check if live file content disagrees with the transcript read
                        for r in reversed(reads):
                            if (
                                r.content
                                and r.start_line is not None
                                and r.end_line is not None
                                and r.start_line <= c_start
                                and c_end <= r.end_line
                            ):
                                r_lines = split_lf_lines(r.content)
                                rel_s = c_start - r.start_line
                                rel_e = c_end - r.start_line + 1
                                if rel_e <= len(r_lines):
                                    expected_slice = r_lines[rel_s:rel_e]
                                    live_slice = live_lines[c_start - 1 : c_end]
                                    if expected_slice != live_slice:
                                        drift_reason = "live_file_differs_from_transcript_read"
                                        findings.append(
                                            Finding(
                                                severity="error",
                                                code="STALE_FILE_READ",
                                                claim=claim,
                                                evidence={
                                                    "file_path": norm_path,
                                                    "claimed_range": [c_start, c_end],
                                                    "read_event_index": r.event_index,
                                                    "reason": drift_reason,
                                                },
                                                message=(
                                                    f"Live content of `{norm_path}` at lines "
                                                    f"{c_start}-{c_end} differs from what was read "
                                                    f"at event #{r.event_index}."
                                                ),
                                                suggestion=(
                                                    f"Re-read {norm_path} lines {c_start}-{c_end}; "
                                                    f"the file changed on disk since it was read."
                                                ),
                                            )
                                        )
                                break
                elif reads and not edits and not stale_fired:
                    # File-level live drift check
                    r = reads[-1]
                    if r.content and r.start_line is not None and r.end_line is not None:
                        r_lines = split_lf_lines(r.content)
                        live_slice = live_lines[r.start_line - 1 : r.start_line - 1 + len(r_lines)]
                        if r_lines and live_slice != r_lines:
                            findings.append(
                                Finding(
                                    severity="warning",
                                    code="STALE_FILE_READ",
                                    claim=claim,
                                    evidence={
                                        "file_path": norm_path,
                                        "read_event_index": r.event_index,
                                        "reason": "live_file_differs_from_transcript_read",
                                    },
                                    message=(
                                        f"Live file `{norm_path}` has been modified on disk since "
                                        f"it was read at event #{r.event_index}."
                                    ),
                                    suggestion=(
                                        f"Re-read {norm_path} to verify against current disk state."
                                    ),
                                )
                            )

    return findings

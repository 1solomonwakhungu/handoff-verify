"""File pointer claim verifier (read coverage, line windows, staleness, live repo)."""

from __future__ import annotations

import contextlib
import posixpath
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
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
from handoff_verify.parsers._common import (
    _resolve_path_with_cd,
    _safe_shlex_split,
    _scan_shell_redirections,
    _split_shell_segments,
    _strip_heredocs,
    _update_active_subdir,
    safe_int,
    split_lf_lines,
)
from handoff_verify.verifiers._paths import (
    KNOWN_EXTENSIONLESS_FILES,
    build_base_dirs,
    is_basename_path_match,
    is_exact_path_match,
    is_suffix_path_match,
    normalize_path,
    paths_match,
    resolve_in_repo,
)
from handoff_verify.verifiers._redirects import RedirectProvenance, collect_redirected_reads

_T = TypeVar("_T")

_GREP_CONTENT_LINE_RE = re.compile(r"^([^\s:][^:\n]*):(\d+)[:-]")
_DASH_NUM_DASH_RE = re.compile(r"-(\d+)-")
_ISO_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\b")
_BARE_LINENUM_LINE_RE = re.compile(r"^(\d+)[:-]")
_SIMPLE_COLON_LINE_END_RE = re.compile(r":(\d{1,6})$")
_PY_TRACEBACK_FILE_RE = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+)')
_EDIT_SNIPPET_LINE_RE = re.compile(r"^\s*(\d+)[→\t]")
_DIFF_NEW_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?([^\t\n]+)")
_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_GREP_ADJACENCY_RADIUS = 2
_GREP_BRIDGE_MAX_GAP = 4
_SUBAGENT_TOOL_NAMES = frozenset({"agent", "task"})
_EDIT_SUMMARY_VERB_RE = re.compile(
    r"\b(?:updated?|edited?|modif(?:y|ied)|fixed?|changed?|patched?|"
    r"added?|removed?|deleted?|refactored?|wrote|written|created?|applied?|replaced?|"
    r"renamed?|moved?|migrated?|relocated?)\b",
    re.IGNORECASE,
)
_DELETE_SUMMARY_VERB_RE = re.compile(
    r"\b(?:removed?|deleted?|dropped?|cleaned\s+up|unlinked?|rm|"
    r"renamed?|moved?|migrated?|relocated?|replaced?|now\s+live(?:s)?\s+in)\b",
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


def _edit_result_snippet_lines(ev: FileEditEvent) -> set[int]:
    """Extract 1-based line numbers shown in an Edit tool result's `cat -n` snippet."""
    lines_seen: set[int] = set()
    if ev.raw_output:
        for ln in split_lf_lines(ev.raw_output):
            m = _EDIT_SNIPPET_LINE_RE.match(ln)
            if m:
                num = int(m.group(1))
                if num >= 1:
                    lines_seen.add(num)
    return lines_seen


def _filter_matching_events(
    items: list[_T],
    path_getter: Callable[[_T], str],
    claimed_path: str,
    base_dirs: tuple[str, ...],
) -> list[_T]:
    """3-tier path match: exact -> suffix (unique or same-tier) -> basename fallback.

    Never falls back from an ambiguous suffix match (`src/pkg/app.py`, `other/pkg/app.py`)
    to a less-specific bare root file (`app.py`).
    """
    exact = [
        item
        for item in items
        if path_getter(item)
        and is_exact_path_match(claimed_path, path_getter(item), base_dirs=base_dirs)
    ]
    if exact:
        return exact

    # B2: relative-vs-relative segment-boundary suffix match (when unique distinct file)
    suffix = [
        item
        for item in items
        if path_getter(item)
        and is_suffix_path_match(claimed_path, path_getter(item), base_dirs=base_dirs)
    ]
    if suffix:
        distinct = {normalize_path(path_getter(item), base_dirs=base_dirs) for item in suffix}
        if len(distinct) == 1:
            return suffix
        # Ambiguous multi-segment suffix: stay within the suffix tier rather than falling
        # through to a less-specific root basename match.
        return []

    basename = [
        item
        for item in items
        if path_getter(item)
        and is_basename_path_match(claimed_path, path_getter(item), base_dirs=base_dirs)
    ]
    if basename:
        distinct_base = {
            normalize_path(path_getter(item), base_dirs=base_dirs) for item in basename
        }
        if len(distinct_base) == 1:
            return basename
        # Multiple files share the same basename (e.g. monorepo `index.ts` / `app.py`):
        # keep all candidates so callers can check whether any single file covers the claim
        # without hard-failing on monorepo filenames.
        norm_c = normalize_path(claimed_path, base_dirs=base_dirs)
        if "/" not in norm_c:
            return basename

    return []


def _parse_grep_context_line(line: str, seen_files: set[str]) -> tuple[str, int] | None:
    """Parse `path/to/file-42-content`, supporting hyphens/digits inside filenames."""
    if not line or line[0] in ("-", " ", "\t", ":") or _ISO_DATE_PREFIX_RE.match(line):
        return None
    head = line.split(None, 1)[0]
    for m in _DASH_NUM_DASH_RE.finditer(head):
        cand = head[: m.start()]
        if ":" in cand:
            continue
        if _looks_like_grep_context_path(cand, seen_files):
            with contextlib.suppress(ValueError):
                return cand, int(m.group(1))
    return None


def _is_foreign_shell_edit(
    edit: FileEditEvent,
    norm_claimed: str,
    base_dirs: tuple[str, ...],
) -> bool:
    """Return True for a shell-inferred edit of a same-named file outside the session's tree.

    Agents routinely build scratch copies (`git archive HEAD | tar -x -C /tmp/x`, `cp -R`),
    then `sed -i` / `rm` inside them. Such a path shares a suffix with a repository file but is
    a different file, so it must not make a citation of the repository file look stale. Edit
    and Write tool calls are deliberate and stay matched by suffix (worktree sessions).
    """
    if edit.operation not in ("bash_modify", "delete") or not base_dirs:
        return False
    norm_edit = normalize_path(edit.file_path, base_dirs=base_dirs)
    return norm_edit.startswith("/") and norm_edit != norm_claimed


# --- B5 + B15: Pre-built O(N) output line index & observed path index ---


def _looks_like_grep_context_path(candidate: str, seen_files: set[str]) -> bool:
    """Return True if `candidate` in `candidate-41-text` is a plausible file path."""
    if not candidate or "://" in candidate:
        return False
    if candidate in seen_files:
        return True
    base = posixpath.basename(candidate)
    if not base or base.startswith("."):
        return False
    if base in KNOWN_EXTENSIONLESS_FILES:
        return True
    _, ext = posixpath.splitext(base)
    return bool(ext and len(ext) <= 12 and ext[1:].isalnum())


def _extract_grep_context_window(
    raw_input: dict[str, object] | None,
    command: str = "",
) -> tuple[int, int]:
    """Return explicit (before, after) context lines requested via -A/-B/-C flags."""
    before = 0
    after = 0
    if isinstance(raw_input, dict):
        for key in ("-C", "context", "C"):
            val = safe_int(raw_input.get(key))
            if val is not None and val > 0:
                before = max(before, val)
                after = max(after, val)
        for key in ("-B", "before_context", "B"):
            val = safe_int(raw_input.get(key))
            if val is not None and val > 0:
                before = max(before, val)
        for key in ("-A", "after_context", "A"):
            val = safe_int(raw_input.get(key))
            if val is not None and val > 0:
                after = max(after, val)

    if command:
        stripped, _ = _strip_heredocs(command)
        for comp in _split_shell_segments(stripped, ("&&", "||", ";", "\n")):
            pipes = _split_shell_segments(comp, ("|",))
            if not pipes:
                continue
            clean_first, _ = _scan_shell_redirections(pipes[0])
            toks = _safe_shlex_split(clean_first)
            if not toks:
                continue
            prog = Path(toks[0]).name
            if prog == "git" and len(toks) > 1 and toks[1] == "grep":
                flag_toks = toks[2:]
            elif prog in ("rg", "grep", "egrep", "fgrep", "ag", "ack"):
                flag_toks = toks[1:]
            else:
                continue
            i = 0
            while i < len(flag_toks):
                tok = flag_toks[i]
                if tok == "--":
                    break
                if tok in ("-C", "--context") and i + 1 < len(flag_toks):
                    val = safe_int(flag_toks[i + 1])
                    if val is not None and val > 0:
                        before = max(before, val)
                        after = max(after, val)
                    i += 2
                    continue
                if tok.startswith("--context="):
                    val = safe_int(tok.split("=", 1)[1])
                    if val is not None and val > 0:
                        before = max(before, val)
                        after = max(after, val)
                    i += 1
                    continue
                if tok in ("-B", "--before-context") and i + 1 < len(flag_toks):
                    val = safe_int(flag_toks[i + 1])
                    if val is not None and val > 0:
                        before = max(before, val)
                    i += 2
                    continue
                if tok.startswith("--before-context="):
                    val = safe_int(tok.split("=", 1)[1])
                    if val is not None and val > 0:
                        before = max(before, val)
                    i += 1
                    continue
                if tok in ("-A", "--after-context") and i + 1 < len(flag_toks):
                    val = safe_int(flag_toks[i + 1])
                    if val is not None and val > 0:
                        after = max(after, val)
                    i += 2
                    continue
                if tok.startswith("--after-context="):
                    val = safe_int(tok.split("=", 1)[1])
                    if val is not None and val > 0:
                        after = max(after, val)
                    i += 1
                    continue
                if not tok.startswith("--") and tok.startswith("-") and len(tok) >= 2:
                    # Handle glued flags like -C3, -rnC3, -rnA 3, -B2
                    body = tok[1:]
                    for idx_c, ch in enumerate(body):
                        if ch in ("C", "A", "B"):
                            glued = body[idx_c + 1 :]
                            val = (
                                safe_int(glued)
                                if glued
                                else (
                                    safe_int(flag_toks[i + 1]) if i + 1 < len(flag_toks) else None
                                )
                            )
                            if val is not None and val > 0:
                                if ch in ("C", "B"):
                                    before = max(before, val)
                                if ch in ("C", "A"):
                                    after = max(after, val)
                            if not glued and i + 1 < len(flag_toks):
                                i += 1
                            break
                i += 1

    # Cap explicit context expansion so absurd values cannot blindly ground an entire file
    return min(before, 25), min(after, 25)


def _single_file_grep_target_in_command(command: str) -> str | None:
    """If a compound command has exactly one single-file `grep`/`rg`, return that file path."""
    if not command:
        return None
    stripped, _ = _strip_heredocs(command)
    active_subdir: str | None = None
    targets: list[str] = []
    for comp in _split_shell_segments(stripped, ("&&", "||", ";", "\n")):
        pipes = _split_shell_segments(comp, ("|",))
        if not pipes:
            continue
        clean_first, _ = _scan_shell_redirections(pipes[0])
        toks = _safe_shlex_split(clean_first)
        if not toks:
            continue
        if toks[0] == "cd":
            active_subdir = _update_active_subdir(
                active_subdir, toks[1] if len(toks) >= 2 else None
            )
            continue
        prog = Path(toks[0]).name
        if prog not in ("grep", "rg", "egrep", "fgrep"):
            continue
        # Walk flags simply to collect positional args after query
        pos_args: list[str] = []
        has_e = False
        i = 1
        end_flags = False
        while i < len(toks):
            t = toks[i]
            if not end_flags and t == "--":
                end_flags = True
                i += 1
                continue
            if not end_flags and t.startswith("-"):
                if t in ("-e", "--regexp", "-f", "--file"):
                    has_e = True
                    i += 2
                    continue
                if t in (
                    "-A",
                    "-B",
                    "-C",
                    "-m",
                    "-g",
                    "-t",
                    "-T",
                    "--context",
                    "--after-context",
                    "--before-context",
                    "--max-count",
                    "--glob",
                    "--include",
                    "--exclude",
                    "--exclude-dir",
                    "--color",
                ):
                    i += 2
                    continue
                i += 1
                continue
            pos_args.append(t)
            i += 1
        file_args = pos_args if has_e else pos_args[1:]
        if len(file_args) == 1:
            candidate = _resolve_path_with_cd(file_args[0], active_subdir) or file_args[0]
            base = posixpath.basename(candidate.rstrip("/"))
            if "." in base and not base.startswith("."):
                targets.append(candidate)
        else:
            return None
    return targets[0] if len(targets) == 1 else None


class _OutputLineIndex:
    """Single-pass index of `path:line` observations across searches, commands, and tool outputs."""

    def __init__(
        self,
        transcript: ParsedTranscript,
        base_dirs: tuple[str, ...],
        redirected_reads: list[tuple[FileReadEvent, RedirectProvenance]] = (),
    ) -> None:
        self._base_dirs = base_dirs
        # norm_path -> dict[line_no, max_event_index]
        self._by_path: dict[str, dict[int, int]] = {}
        self._by_basename: dict[str, set[str]] = {}
        self.tool_observed_paths: set[str] = set()
        self._index_transcript(transcript, redirected_reads)

    def _add(self, raw_path: str, line_no: int, event_index: int) -> None:
        if line_no < 1 or not raw_path:
            return
        norm_p = normalize_path(raw_path, base_dirs=self._base_dirs)
        if not norm_p:
            return
        line_map = self._by_path.setdefault(norm_p, {})
        prev = line_map.get(line_no)
        if prev is None or event_index > prev:
            line_map[line_no] = event_index
        base = posixpath.basename(norm_p)
        self._by_basename.setdefault(base, set()).add(norm_p)

    def _add_grep_hits(
        self,
        hits_by_file: dict[str, set[int]],
        event_index: int,
        *,
        ctx_before: int = 0,
        ctx_after: int = 0,
    ) -> None:
        """Credit grep/rg matched lines, context window, and tight consecutive clusters."""
        before = max(_GREP_ADJACENCY_RADIUS, ctx_before)
        after = max(_GREP_ADJACENCY_RADIUS, ctx_after)
        for raw_path, lines in hits_by_file.items():
            ordered = sorted(ln for ln in lines if ln >= 1)
            for idx, ln in enumerate(ordered):
                for adj in range(max(1, ln - before), ln + after + 1):
                    self._add(raw_path, adj, event_index)
                if idx + 1 < len(ordered):
                    nxt = ordered[idx + 1]
                    if 1 < nxt - ln <= _GREP_BRIDGE_MAX_GAP:
                        for mid in range(ln + 1, nxt):
                            self._add(raw_path, mid, event_index)

    def _scan_diff(self, text: str, event_index: int) -> None:
        """Credit new-side line numbers shown in unified diff hunks to their `+++` file.

        Reviewers read `git diff` / `git show` far more often than whole files, and a hunk
        header pins every context and added line to an exact line number in the new file.
        """
        if "@@ -" not in text or "+++ " not in text:
            return
        diff_path: str | None = None
        new_line = 0
        remaining = 0
        for line in split_lf_lines(text):
            m_file = _DIFF_NEW_FILE_RE.match(line)
            if m_file and remaining == 0:
                target = m_file.group(1).strip()
                diff_path = None if target == "/dev/null" else target
                continue
            m_hunk = _DIFF_HUNK_RE.match(line)
            if m_hunk:
                new_line = int(m_hunk.group(1))
                remaining = int(m_hunk.group(2)) if m_hunk.group(2) is not None else 1
                continue
            if remaining <= 0 or diff_path is None:
                continue
            if not line or line[0] in "+ ":
                self._add(diff_path, new_line, event_index)
                new_line += 1
                remaining -= 1
            elif line[0] not in "-\\":
                remaining = 0

    def _scan_text(
        self,
        text: str,
        event_index: int,
        *,
        scan_inline: bool = False,
        ctx_before: int = 0,
        ctx_after: int = 0,
        single_file_target: str | None = None,
        truncated: bool = False,
    ) -> None:
        if not text:
            return
        self._scan_diff(text, event_index)
        grep_hits: dict[str, set[int]] = {}
        seen_match_files: set[str] = set()
        saw_context_lines = False
        for line in split_lf_lines(text):
            m = _GREP_CONTENT_LINE_RE.match(line)
            if m and (not scan_inline or " " not in m.group(1)):
                with contextlib.suppress(ValueError):
                    p = m.group(1)
                    ln = int(m.group(2))
                    seen_match_files.add(p)
                    grep_hits.setdefault(p, set()).add(ln)
                continue
            if not scan_inline:
                ctx_parsed = _parse_grep_context_line(line, seen_match_files)
                if ctx_parsed is not None:
                    p, ln = ctx_parsed
                    # Credit explicit context lines directly (without adding extra +-2 slack).
                    self._add(p, ln, event_index)
                    saw_context_lines = True
                    continue
                if single_file_target and not _ISO_DATE_PREFIX_RE.match(line):
                    m_num = _BARE_LINENUM_LINE_RE.match(line)
                    if m_num:
                        with contextlib.suppress(ValueError):
                            ln = int(m_num.group(1))
                            sep = line[m_num.end() - 1]
                            if sep == ":":
                                grep_hits.setdefault(single_file_target, set()).add(ln)
                            else:
                                self._add(single_file_target, ln, event_index)
                                saw_context_lines = True
                        continue
            m_tb = _PY_TRACEBACK_FILE_RE.search(line)
            if m_tb:
                with contextlib.suppress(ValueError):
                    self._add(m_tb.group(1), int(m_tb.group(2)), event_index)
                continue
            if scan_inline:
                for tok in line.split():
                    if len(tok) > 320 or ":" not in tok:
                        continue
                    cleaned = tok.strip("`\"'()[].,;:!?*")
                    m_end = _SIMPLE_COLON_LINE_END_RE.search(cleaned)
                    if not m_end:
                        continue
                    p_part = cleaned[: m_end.start()]
                    if "/" in p_part and "://" not in p_part:
                        with contextlib.suppress(ValueError):
                            self._add(p_part, int(m_end.group(1)), event_index)

        if grep_hits:
            if scan_inline:
                for p, lns in grep_hits.items():
                    for ln in lns:
                        self._add(p, ln, event_index)
            else:
                # When context lines were already printed and parsed individually (or when
                # output was truncated), do not blindly expand by -C/-A/-B flag values.
                eff_before = 0 if (truncated or saw_context_lines) else min(ctx_before, 5)
                eff_after = 0 if (truncated or saw_context_lines) else min(ctx_after, 5)
                self._add_grep_hits(
                    grep_hits,
                    event_index,
                    ctx_before=eff_before,
                    ctx_after=eff_after,
                )

    def _scan_tool_result_pointers(self, text: str, event_index: int) -> None:
        """Index file pointers (including ranges, #L, and bare filenames) from subagent results."""
        if not text:
            return
        from handoff_verify.extractors import extract_pointer_claims

        for ptr in extract_pointer_claims(text, base_dirs=self._base_dirs):
            if not ptr.file_path:
                continue
            self.tool_observed_paths.add(ptr.file_path)
            if ptr.start_line is not None:
                end = ptr.end_line if ptr.end_line is not None else ptr.start_line
                capped_end = min(end, ptr.start_line + 500)
                for ln in range(ptr.start_line, capped_end + 1):
                    self._add(ptr.file_path, ln, event_index)

    def _index_transcript(
        self,
        transcript: ParsedTranscript,
        redirected_reads: list[tuple[FileReadEvent, RedirectProvenance]],
    ) -> None:
        indexed_ev: set[int] = set()
        for r in transcript.file_reads:
            indexed_ev.add(r.event_index)
        for e in transcript.file_edits:
            indexed_ev.add(e.event_index)

        for search in transcript.searches:
            if not search.has_result or search.is_error or not search.output:
                continue
            indexed_ev.add(search.event_index)
            cmd_str = ""
            if isinstance(search.raw_input, dict):
                raw_cmd = search.raw_input.get("command") or search.raw_input.get("cmd")
                if isinstance(raw_cmd, str):
                    cmd_str = raw_cmd
            ctx_b, ctx_a = _extract_grep_context_window(search.raw_input, cmd_str)
            roots = search.search_paths or ([search.search_path] if search.search_path else [])
            single_target: str | None = None
            if len(roots) == 1:
                base = posixpath.basename(roots[0].rstrip("/"))
                if "." in base and not base.startswith("."):
                    single_target = roots[0]
            self._scan_text(
                search.output,
                search.event_index,
                ctx_before=ctx_b,
                ctx_after=ctx_a,
                single_file_target=single_target,
                truncated=search.truncated,
            )

        for cmd in transcript.commands:
            if not cmd.has_result:
                continue
            indexed_ev.add(cmd.event_index)
            ctx_b, ctx_a = _extract_grep_context_window(cmd.raw_input, cmd.command or "")
            single_target = _single_file_grep_target_in_command(cmd.command or "")
            has_head_pipe = "| head" in (cmd.command or "")
            if cmd.stdout:
                self._scan_text(
                    cmd.stdout,
                    cmd.event_index,
                    ctx_before=ctx_b,
                    ctx_after=ctx_a,
                    single_file_target=single_target,
                    truncated=cmd.truncated or has_head_pipe,
                )
            if cmd.stderr:
                self._scan_text(cmd.stderr, cmd.event_index, scan_inline=True)

        # Index other completed tool outputs (strictly gating full pointer/range relay on
        # Agent/Task subagent results so Read/Edit/WebFetch docs cannot launder line ranges).
        for ev in transcript.events:
            if (
                ev.event_index not in indexed_ev
                and ev.has_result
                and not ev.is_error
                and ev.raw_output
            ):
                if ev.tool_name.lower() in _SUBAGENT_TOOL_NAMES:
                    self._scan_text(ev.raw_output, ev.event_index, scan_inline=True)
                    self._scan_tool_result_pointers(ev.raw_output, ev.event_index)
                else:
                    self._scan_text(ev.raw_output, ev.event_index, scan_inline=True)

        # A diff saved to a file and then read (`git diff > /tmp/pr.diff`, then Read) shows the
        # same hunks; `content` has the Read tool's own line-number gutter already stripped.
        redirected_read_ids = {id(r) for r, _ in redirected_reads}
        for read in transcript.file_reads:
            if read.has_result and not read.is_error and read.content:
                if id(read) in redirected_read_ids:
                    self._scan_text(read.content, read.event_index, truncated=read.truncated)
                else:
                    self._scan_diff(read.content, read.event_index)

    def matching_lines(
        self, claimed_path: str, *, after_event_index: int | None = None
    ) -> set[int]:
        """Return observed 1-based line numbers for `claimed_path` in O(1) amortized time."""
        norm_c = normalize_path(claimed_path, base_dirs=self._base_dirs)
        if not norm_c:
            return set()
        base = posixpath.basename(norm_c)
        candidate_paths = self._by_basename.get(base)
        if not candidate_paths:
            return set()

        matched_paths: list[str] = []
        if norm_c in candidate_paths:
            matched_paths = [norm_c]
        else:
            exact = [
                p
                for p in candidate_paths
                if is_exact_path_match(norm_c, p, base_dirs=self._base_dirs)
            ]
            if exact:
                matched_paths = exact
            else:
                suffix = [
                    p
                    for p in candidate_paths
                    if is_suffix_path_match(norm_c, p, base_dirs=self._base_dirs)
                ]
                if len(suffix) == 1:
                    matched_paths = suffix
                elif not suffix:
                    basename_matches = [
                        p
                        for p in candidate_paths
                        if is_basename_path_match(norm_c, p, base_dirs=self._base_dirs)
                    ]
                    if len(basename_matches) == 1 or "/" not in norm_c:
                        matched_paths = basename_matches

        out: set[int] = set()
        for p in matched_paths:
            for ln, ev_idx in self._by_path[p].items():
                if after_event_index is None or ev_idx > after_event_index:
                    out.add(ln)
        return out


def _collect_search_observed_paths(
    searches: list[SearchEvent],
    base_dirs: tuple[str, ...],
) -> set[str]:
    """Collect normalized file paths observed via Glob/Grep/LS (joining dir + bare entries)."""
    observed: set[str] = set()
    for search in searches:
        if not search.has_result or search.is_error:
            continue
        roots = search.search_paths or ([search.search_path] if search.search_path else [])
        for sp in roots:
            norm_sp = normalize_path(sp, base_dirs=base_dirs)
            if norm_sp:
                observed.add(norm_sp)
        for mf in search.matched_files:
            norm_mf = normalize_path(mf, base_dirs=base_dirs)
            if norm_mf:
                observed.add(norm_mf)
                # B5: `LS` tool / `ls src/` lists bare filenames relative to `search_path`
                if "/" not in norm_mf:
                    for sp in roots:
                        norm_sp = normalize_path(sp, base_dirs=base_dirs)
                        if norm_sp and norm_sp != ".":
                            observed.add(posixpath.join(norm_sp, norm_mf))
    return observed


def _collect_ls_joined_paths(
    commands: list[CommandEvent],
    base_dirs: tuple[str, ...],
) -> set[str]:
    """Join `ls <dir>` stdout bare filenames with `<dir>` so `ls src/` observes `src/a.py`."""
    observed: set[str] = set()
    for cmd in commands:
        if not cmd.has_result or cmd.is_error or not cmd.command or not cmd.stdout:
            continue
        try:
            tokens = shlex.split(cmd.command)
        except ValueError:
            continue
        if not tokens or posixpath.basename(tokens[0]) != "ls":
            continue
        dirs = [t for t in tokens[1:] if not t.startswith("-")]
        if len(dirs) != 1:
            continue
        norm_dir = normalize_path(dirs[0], base_dirs=base_dirs)
        if not norm_dir or norm_dir == ".":
            continue
        for line in split_lf_lines(cmd.stdout):
            for entry in line.split():
                clean = entry.strip().rstrip("/")
                if clean and "/" not in clean and not clean.startswith("total"):
                    observed.add(posixpath.join(norm_dir, clean))
    return observed


def _file_in_path_set(
    claimed_path: str,
    observed_paths: set[str],
    base_dirs: tuple[str, ...],
) -> bool:
    norm_c = normalize_path(claimed_path, base_dirs=base_dirs)
    if norm_c in observed_paths:
        return True
    return any(paths_match(norm_c, obs, base_dirs=base_dirs) for obs in observed_paths)


def _collect_moved_paths(
    commands: list[CommandEvent],
    base_dirs: tuple[str, ...],
) -> tuple[set[str], set[str]]:
    """Collect (moved_sources, moved_destinations) from completed `mv` / `git mv` commands."""
    sources: set[str] = set()
    dests: set[str] = set()
    for cmd in commands:
        if not cmd.has_result or cmd.is_error or not cmd.command:
            continue
        try:
            tokens = shlex.split(cmd.command)
        except ValueError:
            continue
        if not tokens:
            continue
        prog = posixpath.basename(tokens[0])
        if prog == "git" and len(tokens) >= 4 and tokens[1] == "mv":
            args = [t for t in tokens[2:] if not t.startswith("-")]
        elif prog == "mv" and len(tokens) >= 3:
            args = [t for t in tokens[1:] if not t.startswith("-")]
        else:
            continue
        if len(args) >= 2:
            for src in args[:-1]:
                norm_s = normalize_path(src, base_dirs=base_dirs)
                if norm_s:
                    sources.add(norm_s)
            norm_d = normalize_path(args[-1], base_dirs=base_dirs)
            if norm_d:
                dests.add(norm_d)
    return sources, dests


def _file_mentioned_in_commands_or_tools(
    transcript: ParsedTranscript,
    claimed_path: str,
    base_dirs: tuple[str, ...],
) -> bool:
    """Return True if a completed command or tool output mentions the file path."""
    norm_c = normalize_path(claimed_path, base_dirs=base_dirs)
    if not norm_c:
        return False
    pattern = re.compile(r"(?:^|[\s`'\"(/:=])" + re.escape(norm_c) + r"(?:$|[\s`'\"):,;\r\n])")
    for cmd in transcript.commands:
        if not cmd.has_result:
            continue
        if not cmd.is_error and cmd.command and pattern.search(cmd.command):
            return True
        if not cmd.is_error and cmd.stdout and pattern.search(cmd.stdout):
            return True
    # NB: also check completed non-errored tool outputs (e.g., Task/subagent relaying file path)
    for ev in transcript.events:
        if ev.has_result and not ev.is_error and ev.raw_output and pattern.search(ev.raw_output):
            return True
    return False


# --- B1: Sequential Edit Simulation for Precise Stale-Read Analysis ---


@dataclass
class _EditSimulation:
    """Result of simulating sequential edits after the last FileReadEvent."""

    deleted: bool = False
    unlocated_or_bash: bool = False
    earliest_shifted_line: int | None = None
    known_safe_ranges: list[tuple[int, int]] = None  # type: ignore[assignment]
    post_edit_observed_lines: set[int] = None  # type: ignore[assignment]
    last_edit: FileEditEvent | None = None

    def __post_init__(self) -> None:
        if self.known_safe_ranges is None:
            self.known_safe_ranges = []
        if self.post_edit_observed_lines is None:
            self.post_edit_observed_lines = set()


def _simulate_edits_after_read(
    last_read: FileReadEvent | None,
    edits_after_read: list[FileEditEvent],
    post_edit_grep_lines: set[int],
) -> _EditSimulation:
    """Track which line ranges stayed valid vs shifted across edits after the last read."""
    sim = _EditSimulation(
        post_edit_observed_lines=set(post_edit_grep_lines),
        last_edit=edits_after_read[-1] if edits_after_read else None,
    )
    if not edits_after_read:
        return sim

    # Include `cat -n` snippet lines from the final edit's raw_output (if any)
    sim.post_edit_observed_lines.update(_edit_result_snippet_lines(edits_after_read[-1]))

    sim_lines: list[str] | None = None
    sim_start = 1
    if last_read is not None and last_read.content and last_read.start_line is not None:
        sim_lines = split_lf_lines(last_read.content)
        sim_start = last_read.start_line

    known_ranges: list[tuple[int, int]] = []
    earliest_shifted: int | None = None

    for edit in edits_after_read:
        if edit.operation == "write":
            sim.deleted = False
            sim.unlocated_or_bash = False
            earliest_shifted = None
            sim_lines = split_lf_lines(edit.content or "")
            sim_start = 1
            known_ranges = [(1, max(1, len(sim_lines)))]
            continue

        if edit.operation == "delete":
            sim.deleted = True
            sim_lines = None
            known_ranges = []
            continue

        if edit.operation == "bash_modify":
            sim.unlocated_or_bash = True
            earliest_shifted = 1
            known_ranges = []
            sim_lines = None
            continue

        # Standard `edit` operation
        if sim_lines is None or not edit.old_text:
            sim.unlocated_or_bash = True
            earliest_shifted = 1
            known_ranges = []
            continue

        joined = "\n".join(sim_lines)
        pos = joined.find(edit.old_text)
        if pos == -1:
            sim.unlocated_or_bash = True
            earliest_shifted = 1
            known_ranges = []
            continue

        start_offset_lines = joined[:pos].count("\n")
        edit_start_line = sim_start + start_offset_lines
        old_line_count = len(split_lf_lines(edit.old_text))
        new_text = edit.new_text if edit.new_text is not None else ""
        new_line_count = len(split_lf_lines(new_text))
        delta = new_line_count - old_line_count

        # Apply replacement so subsequent sequential edits / MultiEdit sub-edits locate cleanly
        updated_joined = joined[:pos] + new_text + joined[pos + len(edit.old_text) :]
        sim_lines = split_lf_lines(updated_joined)

        new_end_line = edit_start_line + max(1, new_line_count) - 1

        # Shift any previously tracked safe edited ranges that sit below this edit
        if delta != 0:
            shifted_prev: list[tuple[int, int]] = []
            for rs, re_ in known_ranges:
                if re_ < edit_start_line:
                    shifted_prev.append((rs, re_))
                elif rs > edit_start_line + old_line_count - 1:
                    shifted_prev.append((rs + delta, re_ + delta))
            known_ranges = shifted_prev
            shift_point = new_end_line + 1
            if earliest_shifted is None or shift_point < earliest_shifted:
                earliest_shifted = shift_point

        known_ranges.append((edit_start_line, new_end_line))

    sim.earliest_shifted_line = earliest_shifted
    sim.known_safe_ranges = _merge_windows(known_ranges)
    return sim


def _is_line_range_stale(sim: _EditSimulation, c_start: int, c_end: int) -> bool:
    """Return True if [c_start, c_end] was invalidated/shifted by edits since the last read."""
    if sim.deleted:
        return True
    # Explicitly observed post-edit (via Edit `cat -n` result or post-edit Grep -n)
    if sim.post_edit_observed_lines and all(
        ln in sim.post_edit_observed_lines for ln in range(c_start, c_end + 1)
    ):
        return False
    # Directly inside a known edited new-text range (or full Write range)
    if _window_covers(sim.known_safe_ranges, c_start, c_end):
        return False
    if sim.unlocated_or_bash:
        return True
    if sim.earliest_shifted_line is None:
        # All edits preserved line counts -> no lines shifted anywhere in the file!
        return False
    # Untouched lines strictly above the earliest shifting edit did NOT shift
    return c_end >= sim.earliest_shifted_line


def verify_pointers(
    claims: list[Claim],
    transcript: ParsedTranscript,
    *,
    repo_root: Path | None = None,
) -> list[Finding]:
    """Verify file pointer claims against transcript reads, edits, searches, and optional repo."""
    base_dirs = build_base_dirs(transcript.cwd, repo_root)
    synthesized_redirect_reads, redirected_reads = collect_redirected_reads(transcript, base_dirs)
    valid_reads = [
        r for r in transcript.file_reads if r.has_result and not r.is_error and r.file_path
    ] + synthesized_redirect_reads
    valid_edits = [
        e for e in transcript.file_edits if e.has_result and not e.is_error and e.file_path
    ]
    valid_searches = [s for s in transcript.searches if s.has_result and not s.is_error]

    # B15: Build O(N) indices ONCE before looping over claims
    output_line_index = _OutputLineIndex(transcript, base_dirs, redirected_reads=redirected_reads)
    search_observed_paths = _collect_search_observed_paths(valid_searches, base_dirs)
    ls_observed_paths = _collect_ls_joined_paths(transcript.commands, base_dirs)
    moved_sources, moved_dests = _collect_moved_paths(transcript.commands, base_dirs)
    all_listed_paths = (
        search_observed_paths
        | ls_observed_paths
        | moved_sources
        | moved_dests
        | output_line_index.tool_observed_paths
    )

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
            (
                e
                for e in _filter_matching_events(
                    valid_edits, lambda e: e.file_path, claimed_path, base_dirs
                )
                if not _is_foreign_shell_edit(e, norm_path, base_dirs)
            ),
            key=lambda e: e.event_index,
        )
        output_lines = output_line_index.matching_lines(claimed_path) if c_start else set()
        in_searches = _file_in_path_set(claimed_path, all_listed_paths, base_dirs)

        # B4: For slash-less, line-less bare names (e.g. `app.config`, `res.json`, `docs.rs`),
        # require that the name matches an observed transcript path or an actual file in repo_root;
        # otherwise drop to avoid false positives on dotted identifiers/domains.
        if (
            c_start is None
            and "/" not in norm_path
            and norm_path not in KNOWN_EXTENSIONLESS_FILES
            and not reads
            and not edits
            and not in_searches
            and not _file_mentioned_in_commands_or_tools(transcript, claimed_path, base_dirs)
        ):
            if repo_root is None:
                continue
            resolved_check = resolve_in_repo(claimed_path, repo_root, base_dirs=base_dirs)
            if resolved_check is None or not resolved_check.is_file():
                continue

        write_edits = [e for e in edits if e.operation == "write"]
        unread_fired = False

        # --- 1. Unread file check ---
        if c_start is not None:
            assert c_end is not None
            # A range whose first and last lines were both shown (`grep -n` hits at 88 and 95
            # cited as `:88-95`) is anchored in observed output even if the lines between
            # were not printed; only a range with an unseen endpoint is an extrapolation.
            output_covers_range = c_start in output_lines and c_end in output_lines
            if not reads and not write_edits and not output_covers_range:
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
                        message=f"Cited `{norm_path}` ({line_label}), but the file was {reason}.",
                        suggestion=f"Read {line_label} of {norm_path}",
                    )
                )
        else:
            in_commands = (
                False
                if (reads or edits or in_searches)
                else _file_mentioned_in_commands_or_tools(transcript, claimed_path, base_dirs)
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

        # --- 2. Stale read & Line window checks ---
        stale_fired = False
        last_edit = edits[-1] if edits else None
        last_read = reads[-1] if reads else None
        is_deleted_in_transcript = (
            last_edit is not None and last_edit.operation == "delete"
        ) or _file_in_path_set(claimed_path, moved_sources, base_dirs)

        if not unread_fired:
            # NB7: include same-event-index `bash_modify` (`cat f && sed -i ... f`)
            edits_after_read = [
                e
                for e in edits
                if last_read is None
                or e.event_index > last_read.event_index
                or (e.event_index == last_read.event_index and e.operation == "bash_modify")
            ]

            sim: _EditSimulation | None = None
            if edits_after_read:
                post_edit_grep = (
                    output_line_index.matching_lines(
                        claimed_path, after_event_index=edits_after_read[-1].event_index
                    )
                    if c_start is not None
                    else set()
                )
                sim = _simulate_edits_after_read(last_read, edits_after_read, post_edit_grep)

                if sim.deleted:
                    # B6: summarizing a deletion ("Removed `src/a.py`") without line numbers is ok
                    if c_start is not None or not _DELETE_SUMMARY_VERB_RE.search(
                        claim.context or ""
                    ):
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
                                    "last_edit_event_index": edits_after_read[-1].event_index,
                                    "edit_operation": "delete",
                                },
                                message=(
                                    f"Cited `{norm_path}`, which was deleted at event "
                                    f"#{edits_after_read[-1].event_index}."
                                ),
                                suggestion=(
                                    f"Verify whether {norm_path} still exists before citing it."
                                ),
                            )
                        )
                elif c_start is not None:
                    assert c_end is not None
                    if _is_line_range_stale(sim, c_start, c_end):
                        stale_fired = True
                        line_label = (
                            f"line {c_start}" if c_start == c_end else f"lines {c_start}-{c_end}"
                        )
                        last_ed = edits_after_read[-1]
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
                                    "last_edit_event_index": last_ed.event_index,
                                    "edit_operation": last_ed.operation,
                                },
                                message=(
                                    f"Cited `{norm_path}` ({line_label}) using a stale read: "
                                    f"the file was modified at event #{last_ed.event_index} "
                                    f"({last_ed.operation}) and not re-read afterward."
                                ),
                                suggestion=(
                                    f"Re-read {norm_path} after event #{last_ed.event_index} "
                                    f"to verify current line numbers."
                                ),
                            )
                        )
                else:
                    # File-level mention after edit: clean for `write` or edit-summary verbs
                    last_ed = edits_after_read[-1]
                    if last_ed.operation != "write" and not _EDIT_SUMMARY_VERB_RE.search(
                        claim.context or ""
                    ):
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
                                    "last_edit_event_index": last_ed.event_index,
                                    "edit_operation": last_ed.operation,
                                },
                                message=(
                                    f"Referenced `{norm_path}` after modifying it at event "
                                    f"#{last_ed.event_index} ({last_ed.operation}) without "
                                    f"re-reading."
                                ),
                                suggestion=f"Re-read {norm_path} to confirm its post-edit state.",
                            )
                        )

            # Check line window coverage when a line range is cited
            if c_start is not None:
                assert c_end is not None
                post_edit_reads = (
                    [r for r in reads if r.event_index > last_edit.event_index]
                    if last_edit is not None
                    else reads
                )
                windows: list[tuple[int, int]] = []
                unknown_untruncated_read = False

                if last_edit is not None and post_edit_reads:
                    for r in post_edit_reads:
                        w = _read_event_window(r)
                        if w is not None:
                            windows.append(w)
                        elif not r.truncated:
                            unknown_untruncated_read = True
                    if last_edit.operation == "write":
                        ww = _write_event_window(last_edit)
                        if ww is not None:
                            windows.append(ww)
                    for ln in output_line_index.matching_lines(
                        claimed_path, after_event_index=last_edit.event_index
                    ):
                        windows.append((ln, ln))
                    merged_post = _merge_windows(windows)
                    if not unknown_untruncated_read and not _window_covers(
                        merged_post, c_start, c_end
                    ):
                        pre_reads = [r for r in reads if r.event_index <= last_edit.event_index]
                        pre_windows = [
                            w for r in pre_reads if (w := _read_event_window(r)) is not None
                        ]
                        merged_pre = _merge_windows(pre_windows)
                        if _window_covers(merged_pre, c_start, c_end):
                            # R2-1: Simulate edits from the last pre-edit read so unshifted lines
                            # above the edit (or same-line-count edits) stay valid even after a
                            # partial post-edit re-read of another region!
                            last_pre_read = pre_reads[-1] if pre_reads else None
                            intervening_edits = [
                                e
                                for e in edits
                                if last_pre_read is None
                                or e.event_index > last_pre_read.event_index
                                or (
                                    e.event_index == last_pre_read.event_index
                                    and e.operation == "bash_modify"
                                )
                            ]
                            pre_sim = _simulate_edits_after_read(
                                last_pre_read, intervening_edits, set()
                            )
                            if _is_line_range_stale(pre_sim, c_start, c_end) and not stale_fired:
                                stale_fired = True
                                findings.append(
                                    Finding(
                                        severity="error",
                                        code="STALE_FILE_READ",
                                        claim=claim,
                                        evidence={
                                            "file_path": norm_path,
                                            "claimed_range": [c_start, c_end],
                                            "last_edit_event_index": last_edit.event_index,
                                            "post_edit_windows": [[s, e] for s, e in merged_post],
                                        },
                                        message=(
                                            f"Cited `{norm_path}` lines {c_start}-{c_end}, which "
                                            f"were only read before event #{last_edit.event_index} "
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
                    if last_edit is not None and last_edit.operation == "write":
                        ww = _write_event_window(last_edit)
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
                        if sim is not None:
                            windows.extend(sim.known_safe_ranges)
                            for ln in sim.post_edit_observed_lines:
                                windows.append((ln, ln))
                    for ln in output_lines:
                        windows.append((ln, ln))

                    merged = _merge_windows(windows)
                    endpoints_in_output = c_start in output_lines and c_end in output_lines
                    if (
                        not unknown_untruncated_read
                        and merged
                        and not endpoints_in_output
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

        # --- 3. Live repo check (confined strictly inside repo_root) ---
        if repo_root is not None:
            # B6: Never emit FILE_NOT_FOUND for a file deleted in the transcript (line-less mention)
            if is_deleted_in_transcript and c_start is None:
                continue

            observed_paths = [r.file_path for r in reads] + [e.file_path for e in edits]
            resolved = resolve_in_repo(
                claimed_path,
                repo_root,
                base_dirs=base_dirs,
                fallback_observed=observed_paths,
            )
            # NB8: Paths escaping repo_root (e.g. `../secret.py` or `/etc/passwd`) are never opened
            if resolved is None:
                continue

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
                                        findings.append(
                                            Finding(
                                                severity="error",
                                                code="STALE_FILE_READ",
                                                claim=claim,
                                                evidence={
                                                    "file_path": norm_path,
                                                    "claimed_range": [c_start, c_end],
                                                    "read_event_index": r.event_index,
                                                    "reason": (
                                                        "live_file_differs_from_transcript_read"
                                                    ),
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

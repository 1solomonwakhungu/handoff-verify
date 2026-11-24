"""Quoted code snippet verifier (exact, whitespace/operator normalized, lazy corpus)."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from handoff_verify.models import Claim, Finding, ParsedTranscript
from handoff_verify.parsers._common import split_lf_lines
from handoff_verify.verifiers._paths import (
    KNOWN_EXTENSIONLESS_FILES,
    build_base_dirs,
    is_basename_path_match,
    is_exact_path_match,
    is_suffix_path_match,
    normalize_path,
)
from handoff_verify.verifiers._redirects import collect_redirected_reads

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ARROW_PREFIX_RE = re.compile(r"^\s*\d+→")
_TAB_NUM_PREFIX_RE = re.compile(r"^\s*\d+\t")
_GREP_PREFIX_RE = re.compile(r"^((?:[A-Za-z]:[/\\])?[^\s:][^:\n]*):\d+[:-]")
_DASH_NUM_DASH_RE = re.compile(r"-(\d+)-")
_GREP_NO_LINENUM_RE = re.compile(r"^((?:[A-Za-z]:[/\\])?[^\s:]+):(.*)$")
_BARE_LINENUM_PREFIX_RE = re.compile(r"^\d+[:-]")
_COLON_NUM_PREFIX_RE = re.compile(r"^\s*\d+\s*[:|]\s?")
_WS_RUN_RE = re.compile(r"[ \t]+")
_PUNCT_OPEN_SPACE_RE = re.compile(r"([(\[{])\s+")
_PUNCT_CLOSE_SPACE_RE = re.compile(r"\s+([)\]},;])")
_TRAILING_COMMA_RE = re.compile(r",([)\]}])")
_OP_SPACE_RE = re.compile(r"\s*(==|!=|<=|>=|=>|->|:=|\+=|-=|\*=|/=|=|<|>|:|,)\s*")
# R2-2: recognize before/after edit descriptions (`from \`a\` to \`b\``, `\`a\` -> \`b\``, `is now`)
_PAST_TENSE_QUOTE_RE = re.compile(
    r"(?:\b(?:previously|before|used\s+to|formerly|originally|was|had|old|removed|replaced|"
    r"is\s+now)\b|"
    r"\bfrom\s+`[^`]+`\s+to\s+`|"
    r"`[^`]+`\s*(?:→|->|=>)\s*`)",
    re.IGNORECASE,
)
_CALL_SHORTHAND_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\(([^()]*)\)$")
_IDENT_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_grep_context_content(
    line: str, seen_files: set[str] | None = None
) -> tuple[str, str] | None:
    """Parse `path/to/file-42-content` into `(path, content)`, handling hyphenated filenames."""
    if not line or line[0] in ("-", " ", "\t", ":"):
        return None
    head = line.split(None, 1)[0]
    for m in _DASH_NUM_DASH_RE.finditer(head):
        cand = head[: m.start()]
        if ":" in cand:
            continue
        base = posixpath.basename(cand)
        _, ext = posixpath.splitext(base)
        if (
            (seen_files and cand in seen_files)
            or base in KNOWN_EXTENSIONLESS_FILES
            or (ext and len(ext) <= 12 and ext[1:].isalnum())
        ):
            return cand, line[m.end() :]
    return None


def _strip_grep_any_prefix(line: str) -> str | None:
    """Strip `file:line:` or `file-line-` prefix from a grep/rg output line if present."""
    if _GREP_PREFIX_RE.match(line):
        return _GREP_PREFIX_RE.sub("", line, count=1)
    parsed = _parse_grep_context_content(line)
    if parsed is not None:
        return parsed[1]
    return None


def _strip_line_prefixes(lines: list[str]) -> list[str]:
    """Strip consistent line-number prefixes (e.g. `12→`, `12\\t`, `file.py:12:`) from lines."""
    if not lines:
        return lines

    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return lines

    for pat in (_ARROW_PREFIX_RE, _TAB_NUM_PREFIX_RE, _GREP_PREFIX_RE):
        if all(pat.match(ln) for ln in non_empty):
            return [pat.sub("", ln, count=1) if ln.strip() else ln for ln in lines]

    if all(_strip_grep_any_prefix(ln) is not None for ln in non_empty):
        return [(_strip_grep_any_prefix(ln) or ln) if ln.strip() else ln for ln in lines]

    if len(non_empty) >= 2 and all(_COLON_NUM_PREFIX_RE.match(ln) for ln in non_empty):
        return [_COLON_NUM_PREFIX_RE.sub("", ln, count=1) if ln.strip() else ln for ln in lines]

    return lines


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text) if "\x1b" in text else text


def _normalize_lines(text: str) -> list[str]:
    clean_text = _strip_ansi(text)
    raw_lines = _strip_line_prefixes(split_lf_lines(clean_text))
    norm: list[str] = []
    for ln in raw_lines:
        cleaned = _WS_RUN_RE.sub(" ", ln.strip())
        if cleaned:
            norm.append(cleaned)
    return norm


def _compact_code(flat: str) -> str:
    """Normalize spacing around delimiters and operators while preserving all tokens."""
    s = _PUNCT_OPEN_SPACE_RE.sub(r"\1", flat)
    s = _PUNCT_CLOSE_SPACE_RE.sub(r"\1", s)
    s = _TRAILING_COMMA_RE.sub(r"\1", s)
    s = _OP_SPACE_RE.sub(r"\1", s)
    return s


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _bounded_contains(needle: str, haystack: str) -> bool:
    """Substring check enforcing identifier boundaries at start/end of needle.

    Prevents `out = 30` from falsely matching inside `timeout = 30`.
    """
    if not needle:
        return True
    need_left = _is_ident_char(needle[0])
    need_right = _is_ident_char(needle[-1])
    start = 0
    n_len = len(needle)
    h_len = len(haystack)
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return False
        left_ok = (not need_left) or idx == 0 or not _is_ident_char(haystack[idx - 1])
        end_idx = idx + n_len
        right_ok = (not need_right) or end_idx == h_len or not _is_ident_char(haystack[end_idx])
        if left_ok and right_ok:
            return True
        start = idx + 1


@dataclass(frozen=True)
class _NormalizedText:
    raw: str
    lines: tuple[str, ...]
    block: str
    flat: str
    compact: str


def _prepare_text(text: str) -> _NormalizedText:
    norm_lines = tuple(_normalize_lines(text))
    block = "\n".join(norm_lines)
    flat = " ".join(norm_lines)
    compact = _compact_code(flat)
    return _NormalizedText(raw=text, lines=norm_lines, block=block, flat=flat, compact=compact)


@dataclass
class _LazyCorpusEntry:
    source_kind: str  # "file_read", "file_edit", "file_edit_old", "search", "command", "tool"
    file_path: str | None
    event_index: int
    tool_name: str
    raw_text: str
    output_parts: tuple[str, ...] = ()  # command stdout/stderr without the command string
    _prepared: _NormalizedText | None = None

    def prepared(self) -> _NormalizedText:
        if self._prepared is None:
            self._prepared = _prepare_text(self.raw_text)
        return self._prepared


def _ordered_subsequence_match(
    snippet_lines: tuple[str, ...], corpus_lines: tuple[str, ...]
) -> bool:
    """Return True if every normalized snippet line appears in order inside corpus_lines."""
    if len(snippet_lines) < 2 or not corpus_lines:
        return False
    c_idx = 0
    c_len = len(corpus_lines)
    for s_ln in snippet_lines:
        s_compact = _compact_code(s_ln)
        found = False
        while c_idx < c_len:
            c_ln = corpus_lines[c_idx]
            c_idx += 1
            if _bounded_contains(s_ln, c_ln) or (
                len(s_compact) >= 6 and _bounded_contains(s_compact, _compact_code(c_ln))
            ):
                found = True
                break
        if not found:
            return False
    return True


def _matches_call_shorthand(snippet_raw: str, corpus_flat: str) -> bool:
    """Tolerate typed/abbreviated call shorthands like `load(path, strict)`."""
    m = _CALL_SHORTHAND_RE.fullmatch(snippet_raw.strip())
    if not m:
        return False
    func_name = m.group(1)
    args_str = m.group(2).strip()
    if not args_str:
        return False
    # Require the exact function call or definition `func_name(` with identifier boundary
    call_prefix = func_name + "("
    start = 0
    arg_tokens = [
        tok for tok in _IDENT_TOKEN_RE.findall(args_str) if tok not in ("True", "False", "None")
    ]
    if not arg_tokens:
        return False
    while True:
        idx = corpus_flat.find(call_prefix, start)
        if idx == -1:
            return False
        left_ok = idx == 0 or not _is_ident_char(corpus_flat[idx - 1])
        if left_ok:
            # Find matching closing paren within 240 chars
            close_idx = corpus_flat.find(")", idx + len(call_prefix), idx + len(call_prefix) + 240)
            if close_idx != -1:
                actual_args = corpus_flat[idx + len(call_prefix) : close_idx]
                actual_idents = set(_IDENT_TOKEN_RE.findall(actual_args))
                if all(tok in actual_idents for tok in arg_tokens):
                    return True
        start = idx + 1


def _matches(
    snippet: _NormalizedText,
    entry: _LazyCorpusEntry,
    *,
    allow_subsequence: bool = False,
) -> bool:
    """Return True if snippet matches entry exactly or under whitespace/operator normalization."""
    if not snippet.block:
        return True
    if _bounded_contains(snippet.raw, entry.raw_text):
        return True
    corpus = entry.prepared()
    if _bounded_contains(snippet.block, corpus.block):
        return True
    if snippet.flat and _bounded_contains(snippet.flat, corpus.flat):
        return True
    if (
        snippet.compact
        and len(snippet.compact) >= 6
        and _bounded_contains(snippet.compact, corpus.compact)
    ):
        return True
    if _matches_call_shorthand(snippet.raw, corpus.flat):
        return True
    if allow_subsequence and len(snippet.lines) >= 2:
        return _ordered_subsequence_match(snippet.lines, corpus.lines)
    return False


def _append_grep_and_diff_entries(
    entries: list[_LazyCorpusEntry],
    text: str,
    *,
    event_index: int,
    tool_name: str,
    base_dirs: tuple[str, ...],
    single_file_target: str | None = None,
) -> None:
    """Attribute grep/rg (including `-C`/`-A`/`-B` context) and unified diff lines per file."""
    if not text:
        return

    # 1. Unified diff hunks (`+++ b/<path>`)
    if "+++ b/" in text or "diff --git " in text or ("+++ " in text and "@@ -" in text):
        diff_by_file: dict[str, list[str]] = {}
        cur_file: str | None = None
        for ln in split_lf_lines(text):
            if ln.startswith("+++ b/"):
                cur_file = normalize_path(ln[6:].strip(), base_dirs=base_dirs)
                continue
            if ln.startswith("+++ ") and not ln.startswith("+++ /dev/null"):
                cur_file = normalize_path(ln[4:].strip(), base_dirs=base_dirs)
                continue
            if ln.startswith(("diff --git ", "--- ", "@@ ")):
                continue
            if cur_file and ln and ln[0] in ("+", " "):
                diff_by_file.setdefault(cur_file, []).append(ln[1:])
        for norm_fp, dlines in diff_by_file.items():
            entries.append(
                _LazyCorpusEntry(
                    source_kind="command_diff",
                    file_path=norm_fp,
                    event_index=event_index,
                    tool_name=tool_name,
                    raw_text="\n".join(dlines),
                )
            )

    # 2. Grep / rg output lines (`file:line:content` and `file-line-content`)
    by_file: dict[str, list[str]] = {}
    seen_files: set[str] = set()
    for ln in split_lf_lines(text):
        m_grep = _GREP_PREFIX_RE.match(ln)
        if m_grep:
            path_part = m_grep.group(1).strip()
            norm_fp = normalize_path(path_part, base_dirs=base_dirs)
            seen_files.add(path_part)
            content_part = ln[m_grep.end() :]
            by_file.setdefault(norm_fp, []).append(content_part)
            continue
        parsed_ctx = _parse_grep_context_content(ln, seen_files)
        if parsed_ctx is not None:
            cand, content_part = parsed_ctx
            norm_fp = normalize_path(cand, base_dirs=base_dirs)
            by_file.setdefault(norm_fp, []).append(content_part)
            continue
        if single_file_target and _BARE_LINENUM_PREFIX_RE.match(ln):
            content_part = _BARE_LINENUM_PREFIX_RE.sub("", ln, count=1)
            by_file.setdefault(single_file_target, []).append(content_part)
            continue
        m_noln = _GREP_NO_LINENUM_RE.match(ln)
        if m_noln:
            cand_p = m_noln.group(1).strip()
            cand_posix = cand_p.replace("\\", "/")
            cand_base = posixpath.basename(cand_posix)
            if (
                ("/" in cand_posix or "." in cand_base)
                and not cand_p.startswith(("http", "https"))
                and "." in cand_base
                and not cand_base.startswith(".")
            ):
                norm_fp = normalize_path(cand_p, base_dirs=base_dirs)
                by_file.setdefault(norm_fp, []).append(m_noln.group(2))

    for norm_fp, flines in by_file.items():
        entries.append(
            _LazyCorpusEntry(
                source_kind="search",
                file_path=norm_fp,
                event_index=event_index,
                tool_name=tool_name,
                raw_text="\n".join(flines),
            )
        )


def _build_lazy_corpus(
    transcript: ParsedTranscript,
    base_dirs: tuple[str, ...],
) -> list[_LazyCorpusEntry]:
    """Build lightweight raw corpus entries; normalization happens lazily per entry on demand."""
    entries: list[_LazyCorpusEntry] = []

    synthesized_redirect_reads, redirected_reads = collect_redirected_reads(transcript, base_dirs)

    for r in transcript.file_reads:
        if not r.has_result or r.is_error:
            continue
        body = r.content or r.raw_output
        if not body:
            continue
        norm_fp = normalize_path(r.file_path, base_dirs=base_dirs) if r.file_path else None
        entries.append(
            _LazyCorpusEntry(
                source_kind="file_read",
                file_path=norm_fp,
                event_index=r.event_index,
                tool_name=r.tool_name,
                raw_text=body,
            )
        )

    # Credit redirected command outputs that were subsequently read back to their source file(s)
    # and/or as unattributed command output (so reading `/tmp/x.log` doesn't cause QUOTE_MISMATCH).
    for r in synthesized_redirect_reads:
        body = r.content or r.raw_output
        if not body or not r.file_path:
            continue
        entries.append(
            _LazyCorpusEntry(
                source_kind="file_read",
                file_path=normalize_path(r.file_path, base_dirs=base_dirs),
                event_index=r.event_index,
                tool_name=r.tool_name,
                raw_text=body,
            )
        )

    for read_ev, prov in redirected_reads:
        body = read_ev.content or read_ev.raw_output
        if not body:
            continue
        if prov.is_unattributed_output:
            _append_grep_and_diff_entries(
                entries,
                body,
                event_index=read_ev.event_index,
                tool_name=read_ev.tool_name,
                base_dirs=base_dirs,
            )
            entries.append(
                _LazyCorpusEntry(
                    source_kind="command",
                    file_path=None,
                    event_index=read_ev.event_index,
                    tool_name=read_ev.tool_name,
                    raw_text=body,
                    output_parts=(body,),
                )
            )

    for e in transcript.file_edits:
        if not e.has_result or e.is_error:
            continue
        norm_fp = normalize_path(e.file_path, base_dirs=base_dirs) if e.file_path else None
        active_parts = [p for p in (e.content, e.new_text) if p]
        if active_parts:
            entries.append(
                _LazyCorpusEntry(
                    source_kind="file_edit",
                    file_path=norm_fp,
                    event_index=e.event_index,
                    tool_name=e.tool_name,
                    raw_text="\n".join(active_parts),
                )
            )
        if e.old_text:
            entries.append(
                _LazyCorpusEntry(
                    source_kind="file_edit_old",
                    file_path=norm_fp,
                    event_index=e.event_index,
                    tool_name=e.tool_name,
                    raw_text=e.old_text,
                )
            )

    for s in transcript.searches:
        if not s.has_result or s.is_error or not s.output:
            continue
        roots = s.search_paths or ([s.search_path] if s.search_path else [])
        single_file_target: str | None = None
        if len(roots) == 1:
            base = posixpath.basename(roots[0].rstrip("/"))
            if "." in base and not base.startswith("."):
                single_file_target = normalize_path(roots[0], base_dirs=base_dirs)

        _append_grep_and_diff_entries(
            entries,
            s.output,
            event_index=s.event_index,
            tool_name=s.tool_name,
            base_dirs=base_dirs,
            single_file_target=single_file_target,
        )
        entries.append(
            _LazyCorpusEntry(
                source_kind="search",
                file_path=None,
                event_index=s.event_index,
                tool_name=s.tool_name,
                raw_text=s.output,
            )
        )

    for cmd in transcript.commands:
        if not cmd.has_result:
            continue
        if cmd.stdout:
            _append_grep_and_diff_entries(
                entries,
                cmd.stdout,
                event_index=cmd.event_index,
                tool_name=cmd.tool_name,
                base_dirs=base_dirs,
            )

        # B7: include executed command string as well as stdout/stderr so command mentions match
        combined = "\n".join(p for p in (cmd.command, cmd.stdout, cmd.stderr) if p)
        if not combined:
            continue
        entries.append(
            _LazyCorpusEntry(
                source_kind="command",
                file_path=None,
                event_index=cmd.event_index,
                tool_name=cmd.tool_name,
                raw_text=combined,
                output_parts=tuple(p for p in (cmd.stdout, cmd.stderr) if p),
            )
        )

    # B7: include ALL completed tool outputs (including errored tool calls!) so quoting an
    # error message from a failed MCP/WebFetch tool call verifies cleanly.
    indexed_event_indices = {entry.event_index for entry in entries}
    for ev in transcript.events:
        if ev.event_index not in indexed_event_indices and ev.has_result and ev.raw_output:
            entries.append(
                _LazyCorpusEntry(
                    source_kind="tool",
                    file_path=None,
                    event_index=ev.event_index,
                    tool_name=ev.tool_name,
                    raw_text=ev.raw_output,
                )
            )

    return entries


def _observed_in_unattributed_output(
    snippet: _NormalizedText,
    corpus: list[_LazyCorpusEntry],
) -> bool:
    """Return True if the snippet was printed by a command/tool whose source file is unknown.

    Agents read code through shell pipelines the parser cannot attribute to a path
    (`git show rev:f | sed -n 10,40p`, `cat a b`, several commands joined with `;`). A snippet
    found verbatim in that output was observed; which file it came from is unverifiable, so
    it must not be reported as fabricated or misattributed. The command string itself never
    counts, otherwise grepping for a snippet would be enough to "observe" it.
    """
    for entry in corpus:
        if entry.file_path is not None or entry.source_kind not in ("command", "tool", "search"):
            continue
        if not _matches(snippet, entry):
            continue
        if entry.source_kind != "command":
            return True
        output_only = _LazyCorpusEntry(
            source_kind="command",
            file_path=None,
            event_index=entry.event_index,
            tool_name=entry.tool_name,
            raw_text="\n".join(entry.output_parts),
        )
        if entry.output_parts and _matches(snippet, output_only):
            return True
    return False


def _entries_for_file(
    entries: list[_LazyCorpusEntry],
    claimed_path: str,
    base_dirs: tuple[str, ...],
    *,
    include_old: bool,
) -> list[_LazyCorpusEntry]:
    eligible = [
        e
        for e in entries
        if e.file_path is not None and (include_old or e.source_kind != "file_edit_old")
    ]
    exact = [
        e
        for e in eligible
        if is_exact_path_match(claimed_path, e.file_path or "", base_dirs=base_dirs)
    ]
    if exact:
        return exact
    suffix = [
        e
        for e in eligible
        if is_suffix_path_match(claimed_path, e.file_path or "", base_dirs=base_dirs)
    ]
    if suffix:
        distinct = {e.file_path for e in suffix}
        if len(distinct) == 1:
            return suffix
        return []
    basename = [
        e
        for e in eligible
        if is_basename_path_match(claimed_path, e.file_path or "", base_dirs=base_dirs)
    ]
    if basename:
        distinct_base = {e.file_path for e in basename}
        if len(distinct_base) == 1:
            return basename
        norm_c = normalize_path(claimed_path, base_dirs=base_dirs)
        if "/" not in norm_c:
            return basename
    return []


def verify_quotes(
    claims: list[Claim],
    transcript: ParsedTranscript,
    *,
    repo_root: Path | None = None,
) -> list[Finding]:
    """Verify quoted code and command-output snippets against observed transcript content."""
    quote_claims = [c for c in claims if c.kind == "quote" and c.snippet and c.snippet.strip()]
    if not quote_claims:
        return []

    base_dirs = build_base_dirs(transcript.cwd, repo_root)
    corpus = _build_lazy_corpus(transcript, base_dirs)
    findings: list[Finding] = []

    for claim in quote_claims:
        assert claim.snippet is not None
        snippet_norm = _prepare_text(claim.snippet)
        preview = claim.snippet.strip().splitlines()[0][:80]
        is_past_tense = bool(_PAST_TENSE_QUOTE_RE.search(claim.context or ""))

        if claim.file_path:
            norm_path = normalize_path(claim.file_path, base_dirs=base_dirs)
            file_entries = _entries_for_file(
                corpus, claim.file_path, base_dirs, include_old=is_past_tense
            )

            # NB3: If the file was edited and the claim is present-tense, don't let a pre-edit
            # read justify code that was explicitly removed by a later Edit's `old_text`.
            if not is_past_tense:
                removed_entries = [
                    e
                    for e in _entries_for_file(corpus, claim.file_path, base_dirs, include_old=True)
                    if e.source_kind == "file_edit_old"
                ]
                active_edit_entries = [e for e in file_entries if e.source_kind == "file_edit"]
                if removed_entries:
                    in_removed = any(_matches(snippet_norm, e) for e in removed_entries)
                    in_new = any(_matches(snippet_norm, e) for e in active_edit_entries)
                    latest_edit_idx = max(e.event_index for e in removed_entries)
                    post_edit_reads = [
                        e
                        for e in file_entries
                        if e.source_kind == "file_read" and e.event_index > latest_edit_idx
                    ]
                    in_post_read = any(_matches(snippet_norm, e) for e in post_edit_reads)
                    if in_removed and not in_new and not in_post_read:
                        findings.append(
                            Finding(
                                severity="error",
                                code="QUOTE_NOT_FOUND",
                                claim=claim,
                                evidence={
                                    "claimed_file": norm_path,
                                    "reason": "quoted_removed_code",
                                    "snippet_preview": preview,
                                },
                                message=(
                                    f"Quoted snippet `{preview}` was removed from `{norm_path}` "
                                    f"by an edit at event #{latest_edit_idx}."
                                ),
                                suggestion=(
                                    f"Re-read `{norm_path}` and quote the current post-edit code."
                                ),
                            )
                        )
                        continue

            if any(_matches(snippet_norm, entry) for entry in file_entries):
                continue

            if _observed_in_unattributed_output(snippet_norm, corpus):
                continue

            # Not found in the cited file; check if it actually appeared in a DIFFERENT file
            other_match = next(
                (
                    entry
                    for entry in corpus
                    if entry.file_path is not None
                    and entry.source_kind != "file_edit_old"
                    and entry not in file_entries
                    and _matches(snippet_norm, entry)
                ),
                None,
            )
            if other_match is not None:
                findings.append(
                    Finding(
                        severity="error",
                        code="QUOTE_MISMATCH",
                        claim=claim,
                        evidence={
                            "claimed_file": norm_path,
                            "observed_in_file": other_match.file_path,
                            "observed_event_index": other_match.event_index,
                            "snippet_preview": preview,
                        },
                        message=(
                            f"Quoted snippet `{preview}` was attributed to `{norm_path}`, "
                            f"but was only observed in `{other_match.file_path}`."
                        ),
                        suggestion=(
                            f"Verify whether the quote comes from `{other_match.file_path}` or "
                            f"re-read `{norm_path}`."
                        ),
                    )
                )
                continue

            checked_sources = [f"{e.source_kind}#{e.event_index}" for e in file_entries]
            findings.append(
                Finding(
                    severity="error",
                    code="QUOTE_NOT_FOUND",
                    claim=claim,
                    evidence={
                        "claimed_file": norm_path,
                        "checked_events": checked_sources,
                        "snippet_preview": preview,
                    },
                    message=(
                        f"Quoted snippet `{preview}` was not found in observed content of "
                        f"`{norm_path}`."
                        if file_entries
                        else f"Quoted snippet `{preview}` was attributed to `{norm_path}`, "
                        f"which has no observed content in the transcript."
                    ),
                    suggestion=(
                        f"Re-read `{norm_path}` and verify the exact code before quoting it."
                    ),
                )
            )
        else:
            # General quote (command output, tool output, or unattributed fenced block):
            # allow ordered line-subsequence match for non-contiguous multi-line output excerpts.
            if any(
                _matches(
                    snippet_norm,
                    entry,
                    allow_subsequence=entry.source_kind in ("command", "tool", "search"),
                )
                for entry in corpus
                if entry.source_kind != "file_edit_old" or is_past_tense
            ):
                continue

            findings.append(
                Finding(
                    severity="error",
                    code="QUOTE_NOT_FOUND",
                    claim=claim,
                    evidence={
                        "checked_entries": len(corpus),
                        "snippet_preview": preview,
                    },
                    message=(
                        f"Quoted snippet `{preview}` was not found in any observed file read, "
                        f"edit, or command output in the transcript."
                    ),
                    suggestion="Verify the exact tool/file output before quoting it verbatim.",
                )
            )

    return findings

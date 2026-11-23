"""Quoted code snippet verifier (exact and whitespace-normalized matching)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from handoff_verify.models import Claim, Finding, ParsedTranscript
from handoff_verify.parsers._common import split_lf_lines
from handoff_verify.verifiers._paths import (
    build_base_dirs,
    is_exact_path_match,
    normalize_path,
    paths_match,
)

# Line-number prefix patterns commonly copied from Read / cat -n / grep -n outputs
_ARROW_PREFIX_RE = re.compile(r"^\s*\d+→")
_TAB_NUM_PREFIX_RE = re.compile(r"^\s*\d+\t")
_GREP_PREFIX_RE = re.compile(r"^[^\s:][^:\n]*:\d+[:-]")
_COLON_NUM_PREFIX_RE = re.compile(r"^\s*\d+\s*[:|]\s?")
_WS_RUN_RE = re.compile(r"[ \t]+")
_PUNCT_OPEN_SPACE_RE = re.compile(r"([(\[{])\s+")
_PUNCT_CLOSE_SPACE_RE = re.compile(r"\s+([)\]},;])")
_TRAILING_COMMA_RE = re.compile(r",([)\]}])")


def _strip_line_prefixes(lines: list[str]) -> list[str]:
    """Strip consistent line-number prefixes (e.g. `12→`, `12\\t`, `file.py:12:`) from lines."""
    if not lines:
        return lines

    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return lines

    # Check if every non-empty line carries a Claude Code arrow, cat -n tab, or grep prefix
    for pat in (_ARROW_PREFIX_RE, _TAB_NUM_PREFIX_RE, _GREP_PREFIX_RE):
        if all(pat.match(ln) for ln in non_empty):
            return [pat.sub("", ln, count=1) if ln.strip() else ln for ln in lines]

    # For multi-line snippets only, strip `12: code` or `12 | code` if ALL non-empty lines match
    if len(non_empty) >= 2 and all(_COLON_NUM_PREFIX_RE.match(ln) for ln in non_empty):
        return [_COLON_NUM_PREFIX_RE.sub("", ln, count=1) if ln.strip() else ln for ln in lines]

    return lines


def _normalize_lines(text: str) -> list[str]:
    """Normalize text into stripped, single-space-collapsed non-empty lines."""
    raw_lines = _strip_line_prefixes(split_lf_lines(text))
    norm: list[str] = []
    for ln in raw_lines:
        cleaned = _WS_RUN_RE.sub(" ", ln.strip())
        if cleaned:
            norm.append(cleaned)
    return norm


def _compact_code(flat: str) -> str:
    """Normalize spacing around delimiters and trailing commas while preserving all tokens."""
    s = _PUNCT_OPEN_SPACE_RE.sub(r"\1", flat)
    s = _PUNCT_CLOSE_SPACE_RE.sub(r"\1", s)
    s = _TRAILING_COMMA_RE.sub(r"\1", s)
    return s


@dataclass(frozen=True)
class _NormalizedText:
    raw: str
    block: str
    flat: str
    compact: str


def _prepare_text(text: str) -> _NormalizedText:
    norm_lines = _normalize_lines(text)
    block = "\n".join(norm_lines)
    flat = " ".join(norm_lines)
    compact = _compact_code(flat)
    return _NormalizedText(raw=text, block=block, flat=flat, compact=compact)


@dataclass(frozen=True)
class _CorpusEntry:
    source_kind: str  # "file_read", "file_edit", "search", "command", "tool"
    file_path: str | None
    event_index: int
    tool_name: str
    text: _NormalizedText


def _matches(snippet: _NormalizedText, corpus: _NormalizedText) -> bool:
    """Return True if snippet matches corpus exactly or under whitespace normalization."""
    if not snippet.block:
        return True
    if snippet.raw in corpus.raw:
        return True
    if snippet.block in corpus.block:
        return True
    if snippet.flat and snippet.flat in corpus.flat:
        return True
    return bool(snippet.compact and len(snippet.compact) >= 6 and snippet.compact in corpus.compact)


def _build_corpus(
    transcript: ParsedTranscript,
    base_dirs: tuple[str, ...],
) -> list[_CorpusEntry]:
    entries: list[_CorpusEntry] = []

    for r in transcript.file_reads:
        if not r.has_result or r.is_error:
            continue
        body = r.content or r.raw_output
        if not body:
            continue
        norm_fp = normalize_path(r.file_path, base_dirs=base_dirs) if r.file_path else None
        entries.append(
            _CorpusEntry(
                source_kind="file_read",
                file_path=norm_fp,
                event_index=r.event_index,
                tool_name=r.tool_name,
                text=_prepare_text(body),
            )
        )

    for e in transcript.file_edits:
        if not e.has_result or e.is_error:
            continue
        norm_fp = normalize_path(e.file_path, base_dirs=base_dirs) if e.file_path else None
        parts = [p for p in (e.content, e.new_text, e.old_text) if p]
        if not parts:
            continue
        entries.append(
            _CorpusEntry(
                source_kind="file_edit",
                file_path=norm_fp,
                event_index=e.event_index,
                tool_name=e.tool_name,
                text=_prepare_text("\n".join(parts)),
            )
        )

    for s in transcript.searches:
        if not s.has_result or s.is_error or not s.output:
            continue
        # Group grep content lines by file when formatted as `path:line:content`
        by_file: dict[str, list[str]] = {}
        all_lines = split_lf_lines(s.output)
        for ln in all_lines:
            m = _GREP_PREFIX_RE.match(ln)
            if m:
                path_part = ln.split(":", 1)[0].strip()
                norm_fp = normalize_path(path_part, base_dirs=base_dirs)
                content_part = _GREP_PREFIX_RE.sub("", ln, count=1)
                by_file.setdefault(norm_fp, []).append(content_part)
        for norm_fp, flines in by_file.items():
            entries.append(
                _CorpusEntry(
                    source_kind="search",
                    file_path=norm_fp,
                    event_index=s.event_index,
                    tool_name=s.tool_name,
                    text=_prepare_text("\n".join(flines)),
                )
            )
        entries.append(
            _CorpusEntry(
                source_kind="search",
                file_path=None,
                event_index=s.event_index,
                tool_name=s.tool_name,
                text=_prepare_text(s.output),
            )
        )

    for cmd in transcript.commands:
        if not cmd.has_result:
            continue
        combined = "\n".join(p for p in (cmd.stdout, cmd.stderr) if p)
        if not combined:
            continue
        entries.append(
            _CorpusEntry(
                source_kind="command",
                file_path=None,
                event_index=cmd.event_index,
                tool_name=cmd.tool_name,
                text=_prepare_text(combined),
            )
        )

    # Also index other completed non-errored tool outputs (e.g., MCP / WebFetch)
    indexed_event_indices = {entry.event_index for entry in entries}
    for ev in transcript.events:
        if (
            ev.event_index not in indexed_event_indices
            and ev.has_result
            and not ev.is_error
            and ev.raw_output
        ):
            entries.append(
                _CorpusEntry(
                    source_kind="tool",
                    file_path=None,
                    event_index=ev.event_index,
                    tool_name=ev.tool_name,
                    text=_prepare_text(ev.raw_output),
                )
            )

    return entries


def _entries_for_file(
    entries: list[_CorpusEntry],
    claimed_path: str,
    base_dirs: tuple[str, ...],
) -> list[_CorpusEntry]:
    exact = [
        e
        for e in entries
        if e.file_path is not None
        and is_exact_path_match(claimed_path, e.file_path, base_dirs=base_dirs)
    ]
    if exact:
        return exact
    return [
        e
        for e in entries
        if e.file_path is not None and paths_match(claimed_path, e.file_path, base_dirs=base_dirs)
    ]


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
    corpus = _build_corpus(transcript, base_dirs)
    findings: list[Finding] = []

    for claim in quote_claims:
        assert claim.snippet is not None
        snippet_norm = _prepare_text(claim.snippet)
        preview = claim.snippet.strip().splitlines()[0][:80]

        if claim.file_path:
            norm_path = normalize_path(claim.file_path, base_dirs=base_dirs)
            file_entries = _entries_for_file(corpus, claim.file_path, base_dirs)

            if any(_matches(snippet_norm, entry.text) for entry in file_entries):
                continue

            # Not found in the cited file; check if it actually appeared in a DIFFERENT file
            other_match = next(
                (
                    entry
                    for entry in corpus
                    if entry.file_path is not None
                    and entry not in file_entries
                    and _matches(snippet_norm, entry.text)
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

            # Also check command stdout (e.g., error output tied to file mention)
            cmd_match = next(
                (
                    entry
                    for entry in corpus
                    if entry.source_kind == "command" and _matches(snippet_norm, entry.text)
                ),
                None,
            )
            if cmd_match is not None:
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
            # General quote (command output or unattributed fenced block)
            if any(_matches(snippet_norm, entry.text) for entry in corpus):
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

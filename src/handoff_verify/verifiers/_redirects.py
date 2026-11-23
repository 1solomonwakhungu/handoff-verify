"""Provenance tracking for shell commands whose output is redirected to a file and read back."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from handoff_verify.models import CommandEvent, FileReadEvent, ParsedTranscript
from handoff_verify.parsers._common import (
    _SED_RANGE_RE,
    _awk_input_paths,
    _find_unquoted_heredocs,
    _git_displayed_paths,
    _resolve_path_with_cd,
    _safe_shlex_split,
    _split_shell_segments,
    _strip_heredocs,
    _update_active_subdir,
    parse_numbered_lines,
    safe_int,
)
from handoff_verify.verifiers._paths import is_exact_path_match, normalize_path

_GIT_CONTENT_SUBCMDS = frozenset({"show", "diff", "blame", "log"})
_PURE_WRITE_CMDS = frozenset({"echo", "printf", "touch", "true", ":"})


@dataclass(frozen=True)
class RedirectSource:
    """A source file whose content was redirected into a log/temp file."""

    file_path: str
    start_line: int | None = None
    end_line: int | None = None
    has_line_numbers: bool = False
    exact_line_mapping: bool = False


@dataclass(frozen=True)
class RedirectProvenance:
    """Metadata describing a command that wrote redirected output into `target_path`."""

    target_path: str
    event_index: int
    order: int
    sources: tuple[RedirectSource, ...] = ()
    is_unattributed_output: bool = False
    append: bool = False
    is_tombstone: bool = False


def _scan_redirections_with_mode(segment: str) -> tuple[str, list[tuple[str, bool]]]:
    """Extract unquoted output redirections as ``(target_path, is_append)`` pairs."""
    out: list[str] = []
    targets: list[tuple[str, bool]] = []
    quote: str | None = None
    i = 0
    n = len(segment)

    while i < n:
        ch = segment[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 1
                out.append(segment[i])
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(segment[i + 1])
            i += 2
            continue

        if ch == "<":
            if segment.startswith(("<<<", "<<-"), i):
                in_op_len = 3
            elif segment.startswith(("<<", "<&", "<>"), i):
                in_op_len = 2
            else:
                in_op_len = 1
            j = i + in_op_len
            while j < n and segment[j].isspace():
                j += 1
            tok_quote_in: str | None = None
            while j < n:
                c = segment[j]
                if tok_quote_in:
                    if c == "\\" and tok_quote_in == '"' and j + 1 < n:
                        j += 2
                        continue
                    if c == tok_quote_in:
                        tok_quote_in = None
                    j += 1
                    continue
                if c in ("'", '"'):
                    tok_quote_in = c
                    j += 1
                    continue
                if c.isspace() or c in (";", "|", "&", "<", ">"):
                    break
                j += 1
            out.append(" ")
            i = j
            continue

        redir_start = i
        op_len = 0
        is_stderr_only = False
        if ch == ">":
            op_len = 2 if segment.startswith((">>", ">|", ">&"), i) else 1
        elif ch == "&" and segment.startswith(("&>>", "&>"), i):
            op_len = 3 if segment.startswith("&>>", i) else 2
        elif (
            ch.isdigit()
            and (i == 0 or segment[i - 1].isspace())
            and i + 1 < n
            and segment[i + 1] == ">"
        ):
            is_stderr_only = ch == "2"
            op_len = 3 if segment.startswith((">>", ">|", ">&"), i + 1) else 2

        if op_len:
            op = segment[redir_start : redir_start + op_len]
            is_append = op.endswith(">>")
            j = redir_start + op_len
            while j < n and segment[j].isspace():
                j += 1
            tok_buf: list[str] = []
            tok_quote: str | None = None
            while j < n:
                c = segment[j]
                if tok_quote:
                    if c == "\\" and tok_quote == '"' and j + 1 < n:
                        tok_buf.append(segment[j + 1])
                        j += 2
                        continue
                    if c == tok_quote:
                        tok_quote = None
                        j += 1
                        continue
                    tok_buf.append(c)
                    j += 1
                    continue
                if c in ("'", '"'):
                    tok_quote = c
                    j += 1
                    continue
                if c.isspace() or c in (";", "|", "&", "<", ">"):
                    break
                tok_buf.append(c)
                j += 1

            target = "".join(tok_buf).strip()
            if (
                target
                and not op.endswith(">&")
                and not target.startswith("&")
                and target != "/dev/null"
                and not is_stderr_only
            ):
                targets.append((target, is_append))
            out.append(" ")
            i = j
            continue

        out.append(ch)
        i += 1

    return "".join(out).strip(), targets


def _extract_sed_source(
    tokens: list[str],
    *,
    active_subdir: str | None,
    has_transforming_pipe: bool,
    base_dirs: tuple[str, ...],
) -> RedirectSource | None:
    if "-n" not in tokens or any(t == "-i" or t.startswith("-i") for t in tokens):
        return None
    positionals = [t for t in tokens if not t.startswith("-")]
    if len(positionals) != 2:
        return None
    expr, raw_path = positionals
    m = _SED_RANGE_RE.match(expr.strip())
    if not m:
        return None
    resolved = _resolve_path_with_cd(raw_path, active_subdir) or raw_path
    norm_path = normalize_path(resolved, base_dirs=base_dirs)
    if not norm_path or "$" in norm_path:
        return None
    start = int(m.group(1))
    end_tok = m.group(2)
    explicit_end = int(end_tok) if (end_tok and end_tok != "$") else None
    return RedirectSource(
        file_path=norm_path,
        start_line=start,
        end_line=explicit_end,
        exact_line_mapping=not has_transforming_pipe,
    )


def _extract_cat_head_tail_sources(
    cmd_name: str,
    tokens: list[str],
    *,
    active_subdir: str | None,
    has_transforming_pipe: bool,
    base_dirs: tuple[str, ...],
) -> list[RedirectSource]:
    limit: int | None = None
    has_line_num_flag = cmd_name == "nl"
    files: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if cmd_name in ("head", "tail"):
            if tok in ("-n", "-c") and i + 1 < len(tokens):
                if tok == "-n":
                    limit = safe_int(tokens[i + 1].lstrip("+"))
                i += 2
                continue
            if tok.startswith("-n") and len(tok) > 2:
                limit = safe_int(tok[2:].lstrip("+"))
                i += 1
                continue
            if tok.startswith("-") and tok[1:].isdigit():
                limit = int(tok[1:])
                i += 1
                continue
        elif (
            cmd_name == "nl"
            and tok in ("-b", "-h", "-f", "-s", "-v", "-i", "-w", "-n")
            and i + 1 < len(tokens)
        ):
            i += 2
            continue
        elif cmd_name == "cat" and tok in ("-n", "-b", "--number", "--number-nonblank"):
            has_line_num_flag = True
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        files.append(tok)
        i += 1

    if not files:
        return []

    if cmd_name == "head" and limit is None:
        limit = 10

    exact_single = len(files) == 1 and not has_transforming_pipe and cmd_name != "tail"
    out: list[RedirectSource] = []
    for raw_path in files:
        resolved = _resolve_path_with_cd(raw_path, active_subdir) or raw_path
        norm_path = normalize_path(resolved, base_dirs=base_dirs)
        if not norm_path or "$" in norm_path:
            continue
        out.append(
            RedirectSource(
                file_path=norm_path,
                start_line=1 if exact_single else None,
                end_line=limit if (exact_single and cmd_name == "head") else None,
                has_line_numbers=has_line_num_flag and exact_single,
                exact_line_mapping=exact_single,
            )
        )
    return out


def _analyze_command_redirects(
    cmd: CommandEvent,
    base_dirs: tuple[str, ...],
    order_counter: list[int],
) -> list[RedirectProvenance]:
    """Extract output redirections (`>`, `>>`, `tee`) and tombstones for overwrites."""
    if not cmd.has_result or not cmd.command:
        return []

    cmd_failed = (cmd.is_error and cmd.exit_code != 1) or (cmd.exit_code not in (None, 0, 1))

    stripped_cmd, _ = _strip_heredocs(cmd.command)
    compounds = _split_shell_segments(stripped_cmd, ("&&", "||", ";", "\n"))
    active_subdir: str | None = None
    results: list[RedirectProvenance] = []

    def _next_order() -> int:
        order_counter[0] += 1
        return order_counter[0]

    for comp in compounds:
        pipe_segments = _split_shell_segments(comp, ("|",))
        if not pipe_segments:
            continue

        has_heredoc = bool(_find_unquoted_heredocs(comp))
        clean_pipes: list[str] = []
        all_targets: list[tuple[str, bool]] = []
        for seg in pipe_segments:
            c_seg, seg_redirs = _scan_redirections_with_mode(seg)
            clean_pipes.append(c_seg)
            all_targets.extend(seg_redirs)

        first_tokens = _safe_shlex_split(clean_pipes[0])
        if not first_tokens:
            continue

        if first_tokens[0] == "cd":
            target_dir = first_tokens[1] if len(first_tokens) >= 2 else None
            active_subdir = _update_active_subdir(active_subdir, target_dir)
            continue

        # Also capture `| tee [-a] <file...>` targets
        tee_indices: set[int] = set()
        for idx, seg in enumerate(clean_pipes[1:], start=1):
            p_toks = _safe_shlex_split(seg)
            if p_toks and Path(p_toks[0]).name == "tee":
                tee_indices.add(idx)
                tee_append = any(t in ("-a", "--append") for t in p_toks[1:])
                for tok in p_toks[1:]:
                    if not tok.startswith("-"):
                        all_targets.append((tok, tee_append))

        if not all_targets:
            continue

        cmd_name = Path(first_tokens[0]).name
        subcmd = first_tokens[1] if len(first_tokens) > 1 else ""

        # Pure file-creation commands (heredocs, echo/printf, bare `cat > file`) or failed
        # commands still truncate/overwrite the target file -> emit a tombstone so stale
        # provenance from an earlier redirect into the same path is invalidated.
        is_tombstone = cmd_failed or has_heredoc or cmd_name in _PURE_WRITE_CMDS

        non_tee_downstream = [
            seg for idx, seg in enumerate(clean_pipes[1:], start=1) if idx not in tee_indices
        ]
        has_transforming_pipe = bool(non_tee_downstream)

        sources: list[RedirectSource] = []
        if not is_tombstone:
            if cmd_name == "sed":
                sed_src = _extract_sed_source(
                    first_tokens[1:],
                    active_subdir=active_subdir,
                    has_transforming_pipe=has_transforming_pipe,
                    base_dirs=base_dirs,
                )
                if sed_src is not None:
                    sources.append(sed_src)
            elif cmd_name in ("cat", "nl", "head", "tail"):
                sources.extend(
                    _extract_cat_head_tail_sources(
                        cmd_name,
                        first_tokens[1:],
                        active_subdir=active_subdir,
                        has_transforming_pipe=has_transforming_pipe,
                        base_dirs=base_dirs,
                    )
                )
                if not sources and not has_transforming_pipe:
                    is_tombstone = True
            elif cmd_name == "git" and subcmd in _GIT_CONTENT_SUBCMDS:
                for raw_path in _git_displayed_paths(subcmd, first_tokens[2:]):
                    resolved = _resolve_path_with_cd(raw_path, active_subdir) or raw_path
                    norm_p = normalize_path(resolved, base_dirs=base_dirs)
                    if norm_p and "$" not in norm_p:
                        sources.append(RedirectSource(file_path=norm_p))
            elif cmd_name in ("awk", "gawk", "mawk"):
                for raw_path in _awk_input_paths(first_tokens[1:]):
                    resolved = _resolve_path_with_cd(raw_path, active_subdir) or raw_path
                    norm_p = normalize_path(resolved, base_dirs=base_dirs)
                    if norm_p and "$" not in norm_p:
                        sources.append(RedirectSource(file_path=norm_p))

        is_unattributed = (not is_tombstone) and (
            (not sources) or has_transforming_pipe or len(sources) > 1
        )

        for raw_target, is_append in all_targets:
            if "$" in raw_target or "`" in raw_target:
                continue
            resolved_target = _resolve_path_with_cd(raw_target, active_subdir) or raw_target
            norm_target = normalize_path(resolved_target, base_dirs=base_dirs)
            if not norm_target:
                continue
            results.append(
                RedirectProvenance(
                    target_path=norm_target,
                    event_index=cmd.event_index,
                    order=_next_order(),
                    sources=tuple(sources) if not is_tombstone else (),
                    is_unattributed_output=is_unattributed,
                    append=is_append,
                    is_tombstone=is_tombstone,
                )
            )

    return results


def _active_provenances_for_read(
    read: FileReadEvent,
    provenances: list[RedirectProvenance],
    explicit_writes: list,
    base_dirs: tuple[str, ...],
) -> list[RedirectProvenance]:
    """Return the active chain of redirects into `read.file_path` (from last overwrite to read)."""
    matching = [
        p
        for p in provenances
        if p.event_index <= read.event_index
        and is_exact_path_match(read.file_path, p.target_path, base_dirs=base_dirs)
    ]
    if not matching:
        return []
    matching.sort(key=lambda p: (p.event_index, p.order))

    # Walk backward from the latest redirect until hitting a truncating (non-append) write.
    chain: list[RedirectProvenance] = []
    for prov in reversed(matching):
        chain.append(prov)
        if not prov.append:
            break
    chain.reverse()

    # Check if an explicit Write/Edit/delete overwrote the file after the latest redirect.
    latest_redir_idx = chain[-1].event_index
    overwritten = any(
        latest_redir_idx < e.event_index < read.event_index
        and is_exact_path_match(read.file_path, e.file_path, base_dirs=base_dirs)
        for e in explicit_writes
    )
    if overwritten:
        return []

    # Drop pure tombstones; if the truncating base was a tombstone and nothing non-tombstone
    # appended valid provenance afterward, return empty.
    return [p for p in chain if not p.is_tombstone]


def collect_redirected_reads(
    transcript: ParsedTranscript,
    base_dirs: tuple[str, ...],
) -> tuple[list[FileReadEvent], list[tuple[FileReadEvent, RedirectProvenance]]]:
    """Match redirected output files with subsequent reads of those files.

    Returns ``(synthesized_source_reads, matched_redirected_reads)``:
    - ``synthesized_source_reads``: ``FileReadEvent`` instances credited to the underlying
      source file(s) ONLY when the redirected output file was subsequently read back.
    - ``matched_redirected_reads``: pairs of ``(read_event, provenance)`` for every
      completed ``FileReadEvent`` that read back a redirected command output file.
    """
    provenances: list[RedirectProvenance] = []
    order_counter = [0]
    for cmd in transcript.commands:
        provenances.extend(_analyze_command_redirects(cmd, base_dirs, order_counter))

    if not provenances:
        return [], []

    explicit_writes = [
        e
        for e in transcript.file_edits
        if e.has_result
        and not e.is_error
        and e.file_path
        and e.operation in ("write", "edit", "delete")
    ]

    synthesized: list[FileReadEvent] = []
    matched_reads: list[tuple[FileReadEvent, RedirectProvenance]] = []

    for read in transcript.file_reads:
        if not read.has_result or read.is_error or not read.file_path:
            continue
        active_chain = _active_provenances_for_read(read, provenances, explicit_writes, base_dirs)
        if not active_chain:
            continue

        # Merge chain into one summary provenance for quote/output indexing
        all_sources: list[RedirectSource] = []
        for prov in active_chain:
            all_sources.extend(prov.sources)
        multi_contrib = len(active_chain) > 1 or len(all_sources) > 1
        any_unattributed = any(p.is_unattributed_output for p in active_chain) or multi_contrib
        merged_prov = RedirectProvenance(
            target_path=active_chain[-1].target_path,
            event_index=active_chain[-1].event_index,
            order=active_chain[-1].order,
            sources=tuple(all_sources),
            is_unattributed_output=any_unattributed,
            append=False,
            is_tombstone=False,
        )
        matched_reads.append((read, merged_prov))

        # A partial tail/unknown-window read-back (e.g. `tail -5 /tmp/x`) does NOT prove the
        # entire redirected range was observed; only exact windowed reads map line numbers.
        is_tail_read = read.tool_name.lower().endswith(":tail")
        is_partial_unknown = (read.start_line is None or read.end_line is None) and (
            is_tail_read or read.limit is not None or read.truncated
        )

        for src in all_sources:
            start_line: int | None = None
            end_line: int | None = None
            content = read.content
            line_count = read.line_count
            eff_truncated = read.truncated or is_partial_unknown

            if multi_contrib or is_tail_read:
                # Appended multi-source files or `tail` read-backs cannot safely map 1-based lines
                start_line = None
                end_line = None
            elif src.has_line_numbers and read.content:
                parsed = parse_numbered_lines(read.content, expect_line_numbers=True)
                if parsed.start_line is not None and parsed.end_line is not None:
                    start_line = parsed.start_line
                    end_line = parsed.end_line
                    content = parsed.content
                    line_count = parsed.line_count
            elif (
                src.exact_line_mapping
                and src.start_line is not None
                and read.start_line is not None
                and read.end_line is not None
            ):
                eff_start = src.start_line + read.start_line - 1
                eff_end = src.start_line + read.end_line - 1
                if src.end_line is not None:
                    eff_end = min(eff_end, src.end_line)
                if eff_end >= eff_start >= 1:
                    start_line = eff_start
                    end_line = eff_end

            # Never synthesize an untruncated unknown-window read from a truncated/tail partial
            # read-back (which would falsely accept every line in the file).
            if start_line is None and is_partial_unknown:
                continue

            synthesized.append(
                FileReadEvent(
                    event_id=read.event_id,
                    event_index=read.event_index,
                    tool_name=f"{read.tool_name}:redirect",
                    timestamp=read.timestamp,
                    raw_input=read.raw_input,
                    raw_output=read.raw_output,
                    is_error=False,
                    has_result=True,
                    truncated=eff_truncated,
                    truncation_reason=read.truncation_reason,
                    file_path=src.file_path,
                    start_line=start_line,
                    end_line=end_line,
                    offset=start_line,
                    limit=None,
                    content=content,
                    line_count=line_count,
                )
            )

    return synthesized, matched_reads

"""Deterministic claim extraction from assistant messages.

Extracts structured `Claim` objects for:
- File pointers (`kind="pointer"`): path:line, path:start-end, path:L42, path#L10-L20,
  path (line 42), line 42 of path, markdown links, and backticked repo file paths.
- Quoted code snippets (`kind="quote"`): inline multi-token code snippets tied to a
  cited file or command output, and fenced code blocks claimed from a file/output.
- Absence & completeness claims (`kind="absence"`, `kind="completeness"`): repo-wide
  or explicitly scoped claims about absence of callers/matches or exhaustive coverage.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from handoff_verify.models import AssistantMessage, Claim, ParsedTranscript
from handoff_verify.verifiers._paths import (
    KNOWN_EXTENSIONLESS_FILES,
    build_base_dirs,
    normalize_path,
)

# Known source, config, doc, and data file extensions.
# Deliberately excludes `.config` (which collides with `self.config` / `app.config`).
KNOWN_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".pyx",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".mts",
        ".cts",
        ".json",
        ".jsonl",
        ".jsonc",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".md",
        ".mdx",
        ".rst",
        ".txt",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".sql",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".rb",
        ".erb",
        ".go",
        ".mod",
        ".sum",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".groovy",
        ".gradle",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".hh",
        ".hxx",
        ".cs",
        ".fs",
        ".vb",
        ".php",
        ".swift",
        ".m",
        ".mm",
        ".r",
        ".lua",
        ".pl",
        ".pm",
        ".ex",
        ".exs",
        ".erl",
        ".hrl",
        ".hs",
        ".ml",
        ".mli",
        ".clj",
        ".cljs",
        ".cljc",
        ".edn",
        ".dart",
        ".vue",
        ".svelte",
        ".astro",
        ".tf",
        ".tfvars",
        ".hcl",
        ".proto",
        ".graphql",
        ".gql",
        ".xml",
        ".xsd",
        ".xsl",
        ".svg",
        ".csv",
        ".tsv",
        ".env",
        ".lock",
        ".mk",
        ".cmake",
        ".nix",
        ".zig",
        ".v",
        ".sv",
        ".vhd",
        ".tex",
        ".bib",
        ".diff",
        ".patch",
        ".dockerfile",
        ".ipynb",
        ".jinja",
        ".jinja2",
        ".j2",
        ".tmpl",
        ".tpl",
        ".properties",
        ".plist",
        ".prisma",
        ".sol",
        ".move",
        ".cairo",
        ".wat",
        ".wasm",
        ".log",
    }
)

# Bare dotted names (frameworks, runtime globals, web domains) that are NOT files when
# written without a directory slash or line specification.
_NON_FILE_BARE_NAMES: frozenset[str] = frozenset(
    {
        "node.js",
        "next.js",
        "nuxt.js",
        "vue.js",
        "react.js",
        "express.js",
        "three.js",
        "d3.js",
        "chart.js",
        "alpine.js",
        "solid.js",
        "ember.js",
        "backbone.js",
        "moment.js",
        "day.js",
        "socket.io",
        "asp.net",
        "console.log",
        "process.env",
        "docs.rs",
        "crates.io",
        "e.g",
        "i.e",
    }
)

_CODE_RECEIVER_STEMS: frozenset[str] = frozenset(
    {
        "self",
        "cls",
        "this",
        "super",
        "args",
        "kwargs",
        "opts",
        "req",
        "res",
        "ctx",
        "obj",
        "err",
        "exc",
        "sys",
        "os",
        "console",
        "process",
        "window",
        "document",
        "navigator",
        "logger",
        "logging",
        "docs",
        "www",
    }
)

# Bookkeeping tools that do not gather code evidence and should not hide a preceding summary.
_BOOKKEEPING_TOOLS: frozenset[str] = frozenset(
    {
        "todowrite",
        "todoread",
        "taskcreate",
        "taskupdate",
        "tasklist",
        "taskget",
        "update_plan",
        "memorywrite",
    }
)

_MAX_LINE_NUMBER = 1_000_000
_MAX_PATH_LEN = 300

# --- Markdown segmentation (fenced code blocks vs prose) ---

_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\n]*)$")


@dataclass(frozen=True)
class _FencedBlock:
    start: int
    end: int
    lang: str
    body: str
    preceding_text: str


@dataclass(frozen=True)
class _ProseChunk:
    start: int
    end: int
    text: str


def _segment_markdown(text: str) -> tuple[list[_ProseChunk], list[_FencedBlock]]:
    """Split markdown text into prose chunks and fenced code blocks in O(n) time."""
    prose: list[_ProseChunk] = []
    fences: list[_FencedBlock] = []

    lines = text.splitlines(keepends=True)
    pos = 0
    prose_start = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_start = 0
    fence_lang = ""
    body_lines: list[str] = []

    for line in lines:
        line_len = len(line)
        stripped = line.rstrip("\r\n")
        if not in_fence:
            m = _FENCE_OPEN_RE.match(stripped)
            if m:
                marker = m.group(1)
                info = m.group(2).strip()
                if not (marker[0] == "`" and "`" in info):
                    if pos > prose_start:
                        prose.append(_ProseChunk(prose_start, pos, text[prose_start:pos]))
                    in_fence = True
                    fence_char = marker[0]
                    fence_len = len(marker)
                    fence_start = pos
                    fence_lang = info.split()[0].lower() if info else ""
                    body_lines = []
                    pos += line_len
                    continue
        else:
            close_candidate = stripped.lstrip(" \t")
            close_stripped = close_candidate.rstrip(" \t")
            if len(close_stripped) >= fence_len and set(close_stripped) == {fence_char}:
                preceding_start = max(0, fence_start - 300)
                preceding = text[preceding_start:fence_start]
                fences.append(
                    _FencedBlock(
                        start=fence_start,
                        end=pos + line_len,
                        lang=fence_lang,
                        body="".join(body_lines).rstrip("\r\n"),
                        preceding_text=preceding,
                    )
                )
                in_fence = False
                prose_start = pos + line_len
                pos += line_len
                continue
            body_lines.append(line)
        pos += line_len

    if in_fence:
        if fence_start > prose_start:
            prose.append(_ProseChunk(prose_start, fence_start, text[prose_start:fence_start]))
    elif pos > prose_start:
        prose.append(_ProseChunk(prose_start, pos, text[prose_start:pos]))

    return prose, fences


# --- Target assistant message selection ---


def _events_between_are_bookkeeping(
    transcript: ParsedTranscript,
    start_after: int | None,
    end_after: int | None,
) -> bool:
    """Return True if every event strictly between start_after and end_after is bookkeeping."""
    lo = -1 if start_after is None else start_after
    hi = -1 if end_after is None else end_after
    if hi <= lo:
        return True
    between = [ev for ev in transcript.events if lo < ev.event_index <= hi]
    if not between:
        return False
    return all(ev.tool_name.lower() in _BOOKKEEPING_TOOLS for ev in between)


def _select_target_messages(transcript: ParsedTranscript) -> list[AssistantMessage]:
    """Return final-turn assistant messages, looking past trailing bookkeeping tool calls."""
    if transcript.assistant_messages:
        msgs = transcript.assistant_messages
        selected: list[AssistantMessage] = [msgs[-1]]
        cur_after = msgs[-1].after_event_index
        for msg in reversed(msgs[:-1]):
            if (
                msg.after_event_index == cur_after
                or msg.is_final
                or _events_between_are_bookkeeping(transcript, msg.after_event_index, cur_after)
            ):
                selected.append(msg)
                cur_after = msg.after_event_index
            else:
                break
        selected.reverse()
        return selected

    if transcript.final_text and transcript.final_text.strip():
        return [AssistantMessage(index=0, text=transcript.final_text, is_final=True)]
    return []


# --- Line / sentence splitting ---

_SENTENCE_SPLIT_RE = re.compile(
    r"(?<!\be\.g\.)(?<!\bE\.g\.)(?<!\bi\.e\.)(?<!\bI\.e\.)"
    r"(?<!\betc\.)(?<!\bvs\.)(?<!\bcf\.)(?<!\bno\.)(?<!\bfig\.)"
    r"(?<=[.!?])\s+(?=[A-Z0-9`(\[\"'*])"
)


def _iter_sentences(prose_text: str) -> list[tuple[int, int, str]]:
    """Split a prose chunk into (start, end, sentence_text) tuples within the chunk."""
    results: list[tuple[int, int, str]] = []
    pos = 0
    for raw_line in prose_text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        if line.strip():
            last = 0
            for m in _SENTENCE_SPLIT_RE.finditer(line):
                seg = line[last : m.start()].strip()
                if seg:
                    seg_start = pos + line.find(seg, last)
                    results.append((seg_start, seg_start + len(seg), seg))
                last = m.end()
            tail = line[last:].strip()
            if tail:
                seg_start = pos + line.find(tail, last)
                results.append((seg_start, seg_start + len(tail), tail))
        pos += len(raw_line)
    return results


# --- Path validation helpers ---

_URL_RE = re.compile(r"(?:https?|ftp|file|ssh|ws|wss|mailto)://[^\s<>)\]]+", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_LINE_SUFFIX_RE = re.compile(
    r"(?:"
    r"#L(?P<hstart>\d{1,6})(?:[-–]L?(?P<hend>\d{1,6}))?|"
    r":L?(?P<cstart>\d{1,6})(?:(?:\s*[-–]\s*L?|\.\.)(?P<cend>\d{1,6})|:(?P<col>\d{1,6}))?"
    r")$"
)
_SHELL_VAR_RE = re.compile(r"^\$[A-Z_][A-Z0-9_]*$")


def _is_valid_file_path(
    candidate: str,
    *,
    has_line_spec: bool = False,
) -> bool:
    """Return True if candidate looks like a genuine repo/source file path."""
    s = candidate.strip().strip("`\"'")
    if not s or len(s) > _MAX_PATH_LEN:
        return False
    # Disallow glob wildcards, template angles, braces, shell metachars, whitespace
    if any(ch in s for ch in ("*", "?", "<", ">", "{", "}", "|", ";", "=", " ", "\t", "\n")):
        return False
    if "://" in s or s.startswith(("http:", "https:", "mailto:", "git@")):
        return False
    if s.startswith("-") or s.endswith("/"):
        return False
    if _SHELL_VAR_RE.match(s):
        return False
    if ".." in s.split("/"):
        parts = [p for p in s.split("/") if p and p != ".."]
        if not parts:
            return False

    norm = s.replace("\\", "/")
    base = posixpath.basename(norm)
    if not base or base in (".", ".."):
        return False

    if "/" not in norm and not has_line_spec and base.lower() in _NON_FILE_BARE_NAMES:
        return False
    if all(ch.isdigit() or ch == "." for ch in base):
        return False

    if base in KNOWN_EXTENSIONLESS_FILES:
        return True

    _, ext = posixpath.splitext(base)
    if ext.lower() in KNOWN_FILE_EXTENSIONS:
        stem = base[: -len(ext)]
        if not stem or stem == ".":
            return base.lower() in KNOWN_EXTENSIONLESS_FILES
        if "/" not in norm and not has_line_spec:
            if not any(c.isalpha() for c in stem):
                return False
            if stem.lower() in _CODE_RECEIVER_STEMS:
                return False
        return True

    return base.startswith(".env.")


# --- Hypothetical / Example / Prospective / Non-existence filters ---

_EXAMPLE_PREFIX_RE = re.compile(
    r"(?:^|[\s(\[,;—–-])(?:e\.g\.|i\.e\.|for\s+example|for\s+instance|such\s+as|"
    r"example\s*:|sample\s+path|placeholder|hypothetically|say,\s*$|"
    r"like\s+(?:a\s+)?file\s+such\s+as)\s*[,:]?\s*$",
    re.IGNORECASE,
)

_PROSPECTIVE_PREFIX_RE = re.compile(
    r"(?:^|[\s(\[,;—–-])(?:"
    r"(?:you\s+(?:can|could|should|may|might)|we\s+(?:can|could|should)|"
    r"please|consider|recommend|suggest|try|next\s+step\s*[:—–-]?|todo\s*[:—–-]?)\s+"
    r"(?:to\s+)?(?:create|creating|add|adding|write|writing|open|opening|edit|editing|"
    r"make|making|update|updating|review|reviewing|check|checking|see|modify|modifying|"
    r"bump|bumping|delete|deleting|remove|removing|wire|wiring|document|documenting)\s+"
    r"(?:a\s+(?:new\s+)?)?|"
    r"(?:should|could|would|will|to)\s+(?:create|add|write|update|document|wire)\s+"
    r"(?:a\s+(?:new\s+)?)?|"
    r"(?:^|\b)(?:create|add|document|wire)\s+(?:a\s+new\s+file\s+|the\s+file\s+|it\s+in(?:to)?\s+|coverage\s+in\s+)?|"
    r"new\s+file\s+at\s+"
    r")$",
    re.IGNORECASE,
)

_PROSPECTIVE_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+)?(?:\*\*)?(?:follow[- ]?ups?|next\s+steps?|todos?|"
    r"future\s+work|remaining\s+work|suggested\s+(?:changes?|follow[- ]?ups?))(?:\*\*)?\s*:?\s*$",
    re.IGNORECASE,
)
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")

_PROSPECTIVE_SENTENCE_RE = re.compile(
    r"(?:\b(?:follow[- ]?ups?|next\s+step|todo|you\s+(?:can|could|should)|"
    r"we\s+(?:can|could|should)|consider\s+(?:adding|creating|updating|wiring|documenting)|"
    r"recommend\s+(?:adding|creating|updating)|suggest\s+(?:adding|creating|updating))\b|"
    r"\b(?:later|in\s+a\s+follow[- ]?up)\s*[.!?]?\s*$)",
    re.IGNORECASE,
)

_NEGATED_READ_PREFIX_RE = re.compile(
    r"\b(?:did\s+not|didn't|have\s+not|haven't|has\s+not|hasn't|never|not|without|skip(?:ped)?|"
    r"omitted|excluding)\s+"
    r"(?:read(?:ing)?|open(?:ing)?|inspect(?:ing)?|check(?:ing)?|edit(?:ing)?|"
    r"touch(?:ing)?|modif(?:y|ying)|view(?:ing)?|look(?:ing)?\s+at)\s+$",
    re.IGNORECASE,
)

_NEGATED_READ_SUFFIX_RE = re.compile(
    r"\b(?:"
    r"(?:was|is|were|are)\s+out\s+of\s+scope|"
    r"(?:I|we)\s+(?:did\s+not|didn't|have\s+not|haven't)\s+"
    r"(?:read|open|inspect|check|look\s+at)\s+(?:it|that\s+file|them|those\s+files)"
    r")\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY_RE = re.compile(r"(?:;|\s—\s|\n|\.(?:\s|$))")

_UNTOUCHED_SUFFIX_RE = re.compile(
    r"\b(?:untouched|unchanged|unmodified|as[- ]is|skipped|unread|unopened)\b",
    re.IGNORECASE,
)

_NEGATION_CLAUSE_END = (
    r"(?:\s+(?:in\s+(?:this|the)\s+(?:repo|repository|project|codebase|dir|directory|branch)|"
    r"on\s+disk|anymore|here|yet))?"
    r"(?:\s*$|\s*[,;.)\]]|\s+(?:and|or|but|so|because)\b)"
)

_NONEXISTENT_SUBJECT_SUFFIX_RE = re.compile(
    r"^(?:"
    r"\s*(?:\([^)\n]{1,40}\)\s*)?(?:still\s+|actually\s+)?"
    r"(?:does\s+not\s+(?:actually\s+)?exist|doesn't\s+(?:actually\s+)?exist|"
    r"do\s+not\s+(?:actually\s+)?exist|don't\s+(?:actually\s+)?exist|"
    r"did\s+not\s+(?:actually\s+)?exist|didn't\s+(?:actually\s+)?exist|"
    r"no\s+longer\s+exists?|was\s+not\s+found|wasn't\s+found|"
    r"were\s+not\s+found|weren't\s+found|is\s+not\s+found|isn't\s+found|"
    r"not\s+found|is\s+missing|was\s+missing|are\s+missing|were\s+missing|"
    r"is\s+non-?existent|was\s+never\s+created|"
    r"cannot\s+be\s+found|could\s+not\s+be\s+found|couldn't\s+be\s+found|"
    r"is\s+not\s+present|isn't\s+present)" + _NEGATION_CLAUSE_END + r"|"
    r"\s*(?:—|–|-|:)\s*(?:not\s+found|does\s+not\s+exist|doesn't\s+exist|missing|non-?existent)"
    + _NEGATION_CLAUSE_END
    + r"|"
    r"\s+and\s+\S+(?:\s+\S+){0,2}\s+"
    r"(?:do\s+not\s+exist|don't\s+exist|were\s+not\s+found|weren't\s+found|are\s+missing)"
    + _NEGATION_CLAUSE_END
    + r")",
    re.IGNORECASE,
)

# Line-numbered pointers require explicit existence-negation prefixes so code-review
# phrasings like "The guard was missing: `src/x.py:42`" or "without `src/config.py:12`"
# are still verified.
_NONEXISTENT_LINE_PREFIX_RE = re.compile(
    r"\b(?:"
    r"there\s+(?:is|was|are|were)\s+no(?:\s+(?:such\s+)?(?:file|line|entry|path))?(?:\s+at)?|"
    r"there\s+(?:isn't|wasn't)\s+(?:a|any)(?:\s+(?:file|line|entry|path))?(?:\s+at)?|"
    r"no\s+such(?:\s+file(?:\s+or\s+directory)?)?|"
    r"non-?existent(?:\s+file)?|"
    r"cannot\s+find|could\s+not\s+find|couldn't\s+find"
    r")\s*[:—–-]?\s*$",
    re.IGNORECASE,
)

_NONEXISTENT_BARE_PREFIX_RE = re.compile(
    r"\b(?:"
    r"missing(?:\s+file)?|no|without"
    r")\s*[:—–-]?\s*$",
    re.IGNORECASE,
)

# Locative prepositions before a pointer indicate the pointer is a location modifier
# ("The null check at `src/api/handler.py:42` is missing"), NOT the subject of the
# negation—unless the head noun is explicitly file/path/line/entry/module.
_LOCATIVE_PREP_BEFORE_RE = re.compile(
    r"\b(?:in|at|on|from|inside|within|of)\s+$",
    re.IGNORECASE,
)
_FILE_NOUN_LOCATIVE_BEFORE_RE = re.compile(
    r"\b(?:file|path|line|entry|module)\s+(?:at|in|on)\s+$",
    re.IGNORECASE,
)

_COMMAND_FLAG_PREFIX_RE = re.compile(r"(?:--[a-zA-Z0-9_-]+|-[a-zA-Z])\s+$")

_EXAMPLE_CLAUSE_RE = re.compile(
    r"\b(?:e\.g\.|for\s+example|for\s+instance|such\s+as|hypothetically|placeholder)\b",
    re.IGNORECASE,
)


def _is_hypothetical_pointer(
    sentence: str, match_start: int, match_end: int, has_line: bool
) -> bool:
    """Return True if a pointer match sits in an example, prospective, or negated-read clause."""
    prefix = sentence[max(0, match_start - 90) : match_start]
    for sep in (";", " — ", "\n"):
        idx = prefix.rfind(sep)
        if idx != -1:
            prefix = prefix[idx + len(sep) :]

    if _EXAMPLE_PREFIX_RE.search(prefix):
        return True
    last_open = prefix.rfind("(")
    last_close = prefix.rfind(")")
    if last_open != -1 and last_open > last_close:
        paren_prefix = prefix[last_open + 1 :]
        if _EXAMPLE_CLAUSE_RE.search(paren_prefix):
            return True
    if _EXAMPLE_CLAUSE_RE.search(prefix[-35:]):
        return True

    if _COMMAND_FLAG_PREFIX_RE.search(prefix):
        return True

    if _NEGATED_READ_PREFIX_RE.search(prefix):
        return True

    clause_suffix = sentence[match_end : min(len(sentence), match_end + 110)]
    m_bound = _CLAUSE_BOUNDARY_RE.search(clause_suffix)
    if m_bound is not None:
        clause_suffix = clause_suffix[: m_bound.start()]
    if _NEGATED_READ_SUFFIX_RE.search(clause_suffix):
        return True

    if _PROSPECTIVE_PREFIX_RE.search(prefix) and not re.search(
        r"\b(?:I|we|has|have|was|were)\s+(?:created|added|wrote|updated)\s+$",
        prefix,
        re.IGNORECASE,
    ):
        return True

    # Negated existence claims apply to both line-numbered (`src/app.py:99` does not exist,
    # there is no `foo.py:42`) and bare (`bar.py` wasn't found) file pointers.
    raw_suffix = sentence[match_end : min(len(sentence), match_end + 80)]
    for sep in (";", ".", " — ", "\n"):
        idx = raw_suffix.find(sep)
        if idx != -1:
            raw_suffix = raw_suffix[:idx]
    near_prefix = prefix[-50:]
    if _NONEXISTENT_LINE_PREFIX_RE.search(near_prefix):
        return True
    is_locative_modifier = bool(_LOCATIVE_PREP_BEFORE_RE.search(near_prefix)) and not bool(
        _FILE_NOUN_LOCATIVE_BEFORE_RE.search(near_prefix)
    )
    if not is_locative_modifier and _NONEXISTENT_SUBJECT_SUFFIX_RE.match(raw_suffix):
        return True

    if not has_line:
        if _NONEXISTENT_BARE_PREFIX_RE.search(near_prefix):
            return True
        # R2-9: coordinated prospective sentences ("... add `a.py` and document it in `b.md`")
        if _PROSPECTIVE_SENTENCE_RE.search(sentence) and not re.search(
            r"\b(?:I|we)\s+(?:checked|read|updated|edited|fixed|verified|modified|created|added)\b",
            sentence[:match_start],
            re.IGNORECASE,
        ):
            return True
        if re.search(
            r"\b(?:add|create)\s+\S+(?:\s+\S+){0,4}\s+and\s+(?:document|wire|update|add)\b",
            sentence[:match_start],
            re.IGNORECASE,
        ):
            return True

        if _UNTOUCHED_SUFFIX_RE.search(raw_suffix):
            return True

    return False


# --- Linear-time File Pointer Extraction (ReDoS-free, supports Next.js/SvelteKit paths) ---

_BACKTICK_SPAN_RE = re.compile(r"`([^`\n]{1,320})`")
_WHITESPACE_TOKEN_RE = re.compile(r"\S+")
_AFTER_LINE_RE = re.compile(
    r"^(?:"
    r"\s*\(\s*(?:(?:around|near)\s+)?(?:lines?|L)\s*(?P<pstart>\d{1,6})\b"
    r"(?:\s*[-–]\s*L?(?P<pend>\d{1,6})\b)?\s*\)|"
    r"(?:\s*,\s*|\s+(?:at|around|near)\s+|\s+)(?:lines?\s+|L)(?P<astart>\d{1,6})\b"
    r"(?:\s*[-–]\s*L?(?P<aend>\d{1,6})\b)?(?!\s+(?:of|in)\b)"
    r")",
    re.IGNORECASE,
)
_LINE_OF_PREFIX_RE = re.compile(
    r"\b(?:(?:around|near)\s+)?lines?\s+(?P<start>\d{1,6})(?:\s*[-–]\s*(?P<end>\d{1,6}))?\s+"
    r"(?:of|in)\s+$",
    re.IGNORECASE,
)


def _parse_line_bounds(start_raw: str | None, end_raw: str | None) -> tuple[int, int] | None:
    if not start_raw:
        return None
    start = int(start_raw)
    end = int(end_raw) if end_raw else start
    if start < 1 or end < 1 or start > _MAX_LINE_NUMBER or end > _MAX_LINE_NUMBER:
        return None
    if end < start:
        return None
    return start, end


def _split_path_and_line_suffix(raw: str) -> tuple[str, int | None, int | None]:
    """Split a path token into (path_part, start_line, end_line) in O(len(raw)) time."""
    m = _LINE_SUFFIX_RE.search(raw)
    if not m:
        return raw, None, None
    s_raw = m.group("hstart") or m.group("cstart")
    e_raw = m.group("hend") or m.group("cend")
    bounds = _parse_line_bounds(s_raw, e_raw)
    if bounds is None:
        return raw, None, None
    return raw[: m.start()], bounds[0], bounds[1]


def _trim_bare_token(raw: str) -> tuple[str, int, int]:
    """Strip outer markdown/prose punctuation from a whitespace token while keeping path chars.

    Returns (trimmed_text, left_trim_count, right_trim_count).
    Preserves leading `/` and route-group `(auth)` / `[id]` segments inside paths.
    """
    left = 0
    right = len(raw)
    while left < right and raw[left] in "\"'`<*":
        left += 1
    # Strip leading `(` only if it isn't a Next.js route group like `(auth)/login.tsx`
    while left < right and raw[left] == "(":
        close_idx = raw.find(")", left + 1, right)
        if close_idx != -1 and close_idx + 1 < right and raw[close_idx + 1] == "/":
            break
        left += 1
    while left < right and raw[right - 1] in "\"'`.,;:!?*>":
        right -= 1
    while left < right and raw[right - 1] == ")":
        segment = raw[left:right]
        if segment.count(")") > segment.count("("):
            right -= 1
        else:
            break
    while left < right and raw[right - 1] == "]":
        segment = raw[left:right]
        if segment.count("]") > segment.count("["):
            right -= 1
        else:
            break
    return raw[left:right], left, len(raw) - right


def _span_overlaps_sorted(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    s, e = span
    return any(s < oe and e > os_ for os_, oe in occupied)


def _parse_pointer_from_link(
    label: str,
    target: str,
    *,
    base_dirs: Sequence[str],
) -> tuple[str, int | None, int | None] | None:
    """Extract a single (file_path, start_line, end_line) from a markdown link if repo-local."""
    target_clean = target.strip()
    if re.match(r"^(?:https?|ftp|mailto|ws|wss)://", target_clean, re.IGNORECASE):
        return None
    if target_clean.lower().startswith("file://"):
        target_clean = target_clean[7:]

    t_path_cand, t_start, t_end = _split_path_and_line_suffix(target_clean)
    t_path: str | None = None
    if t_start is not None:
        if _is_valid_file_path(t_path_cand, has_line_spec=True):
            t_path = t_path_cand
    else:
        bare_target = target_clean.split("#", 1)[0]
        if _is_valid_file_path(bare_target):
            t_path = bare_target

    label_stripped = label.strip().strip("`")
    l_path_cand, l_start, l_end = _split_path_and_line_suffix(label_stripped)
    l_path: str | None = None
    if l_start is not None and _is_valid_file_path(l_path_cand, has_line_spec=True):
        l_path = l_path_cand
    elif _is_valid_file_path(label_stripped):
        l_path = label_stripped

    final_path = t_path or l_path
    start = t_start if t_start is not None else l_start
    end = t_end if t_end is not None else l_end
    if not final_path or not _is_valid_file_path(final_path, has_line_spec=start is not None):
        return None
    return normalize_path(final_path, base_dirs=base_dirs), start, end


def extract_pointer_claims(
    text: str,
    *,
    message_index: int | None = None,
    base_dirs: Sequence[str] = (),
) -> list[Claim]:
    """Extract file pointer claims from assistant prose in linear time (no ReDoS)."""
    prose_chunks, _ = _segment_markdown(text)
    claims: list[Claim] = []
    seen_exact: set[tuple[str, int | None, int | None]] = set()
    files_with_line_claims: set[str] = set()
    bare_candidates: list[Claim] = []

    def _record(
        raw_text: str,
        raw_path: str,
        s_line: int | None,
        e_line: int | None,
        sentence: str,
        span: tuple[int, int],
        occupied: list[tuple[int, int]],
    ) -> None:
        occupied.append(span)
        if _is_hypothetical_pointer(sentence, span[0], span[1], has_line=s_line is not None):
            return
        norm_p = normalize_path(raw_path, base_dirs=base_dirs)
        key = (norm_p, s_line, e_line)
        if key in seen_exact:
            return
        seen_exact.add(key)
        c = Claim(
            kind="pointer",
            text=raw_text,
            file_path=norm_p,
            start_line=s_line,
            end_line=e_line,
            context=sentence,
            message_index=message_index,
        )
        if s_line is not None:
            files_with_line_claims.add(norm_p)
            claims.append(c)
        else:
            bare_candidates.append(c)

    for chunk in prose_chunks:
        in_prospective_list = False
        for raw_line in chunk.text.splitlines():
            stripped_line = raw_line.strip()
            if not stripped_line:
                in_prospective_list = False
                continue
            if _PROSPECTIVE_HEADING_RE.match(stripped_line):
                in_prospective_list = True
                continue
            if in_prospective_list and _BULLET_LINE_RE.match(raw_line):
                # R2-9: bullets under "Follow-ups:" / "Next steps:" are prospective tasks
                continue
            if in_prospective_list and not _BULLET_LINE_RE.match(raw_line):
                in_prospective_list = False

            for _, _, sentence in _iter_sentences(raw_line):
                occupied: list[tuple[int, int]] = []

                # 0. Markdown links and external URLs
                for m_link in _MARKDOWN_LINK_RE.finditer(sentence):
                    span = (m_link.start(), m_link.end())
                    parsed = _parse_pointer_from_link(
                        m_link.group(1), m_link.group(2), base_dirs=base_dirs
                    )
                    if parsed is None:
                        occupied.append(span)
                        continue
                    norm_p, s_line, e_line = parsed
                    _record(m_link.group(0), norm_p, s_line, e_line, sentence, span, occupied)

                for m_url in _URL_RE.finditer(sentence):
                    occupied.append((m_url.start(), m_url.end()))

                # 1. Backticked spans (`[id]`, `(auth)`, `+page`, `$id`, `:line`, `#Lline`,
                #    `file.py`:42, `file.py` (line 42), `line 42 of file.py`)
                for m_bt in _BACKTICK_SPAN_RE.finditer(sentence):
                    span = (m_bt.start(), m_bt.end())
                    if _span_overlaps_sorted(span, occupied):
                        continue
                    inner = m_bt.group(1).strip()
                    if not inner or len(inner) > _MAX_PATH_LEN:
                        occupied.append(span)
                        continue

                    path_part, s_line, e_line = _split_path_and_line_suffix(inner)
                    if s_line is not None and _is_valid_file_path(path_part, has_line_spec=True):
                        _record(m_bt.group(0), path_part, s_line, e_line, sentence, span, occupied)
                        continue

                    # Bounded 60-char slices keep scanning strictly O(n) on huge lines
                    after_text = sentence[m_bt.end() : m_bt.end() + 60]
                    before_start = max(0, m_bt.start() - 60)
                    before_text = sentence[before_start : m_bt.start()]
                    m_line_of = _LINE_OF_PREFIX_RE.search(before_text)
                    if m_line_of and _is_valid_file_path(inner, has_line_spec=True):
                        bounds = _parse_line_bounds(
                            m_line_of.group("start"), m_line_of.group("end")
                        )
                        if bounds is not None:
                            full_span = (before_start + m_line_of.start(), m_bt.end())
                            if not _span_overlaps_sorted(full_span, occupied):
                                _record(
                                    sentence[full_span[0] : full_span[1]],
                                    inner,
                                    bounds[0],
                                    bounds[1],
                                    sentence,
                                    full_span,
                                    occupied,
                                )
                                continue

                    # Check `:42` / `#L42` right after closing backtick (`file.py`:42)
                    m_adj_suffix = re.match(
                        r"^(?:#L(?P<hstart>\d{1,6})(?:[-–]L?(?P<hend>\d{1,6}))?|"
                        r":L?(?P<cstart>\d{1,6})(?:(?:\s*[-–]\s*L?|\.\.)(?P<cend>\d{1,6})|:(?P<col>\d{1,6}))?)"
                        r"(?!\d|:\d)",
                        after_text,
                    )
                    if m_adj_suffix and _is_valid_file_path(inner, has_line_spec=True):
                        bounds = _parse_line_bounds(
                            m_adj_suffix.group("hstart") or m_adj_suffix.group("cstart"),
                            m_adj_suffix.group("hend") or m_adj_suffix.group("cend"),
                        )
                        if bounds is not None:
                            full_span = (m_bt.start(), m_bt.end() + m_adj_suffix.end())
                            _record(
                                sentence[full_span[0] : full_span[1]],
                                inner,
                                bounds[0],
                                bounds[1],
                                sentence,
                                full_span,
                                occupied,
                            )
                            continue

                    m_after = _AFTER_LINE_RE.match(after_text)
                    if m_after and _is_valid_file_path(inner, has_line_spec=True):
                        bounds = _parse_line_bounds(
                            m_after.group("pstart") or m_after.group("astart"),
                            m_after.group("pend") or m_after.group("aend"),
                        )
                        if bounds is not None:
                            full_span = (m_bt.start(), m_bt.end() + m_after.end())
                            _record(
                                sentence[full_span[0] : full_span[1]],
                                inner,
                                bounds[0],
                                bounds[1],
                                sentence,
                                full_span,
                                occupied,
                            )
                            continue

                    if _is_valid_file_path(inner, has_line_spec=False):
                        _record(m_bt.group(0), inner, None, None, sentence, span, occupied)
                    else:
                        # Mask non-path backticked code so bare token scanner doesn't pick inside it
                        occupied.append(span)

                # 2. Bare (unbackticked) line-numbered pointers via linear whitespace token scan
                for m_tok in _WHITESPACE_TOKEN_RE.finditer(sentence):
                    raw_tok = m_tok.group(0)
                    if len(raw_tok) > _MAX_PATH_LEN + 30:
                        continue
                    trimmed, l_trim, r_trim = _trim_bare_token(raw_tok)
                    if not trimmed:
                        continue
                    tok_start = m_tok.start() + l_trim
                    tok_end = m_tok.end() - r_trim
                    span = (tok_start, tok_end)
                    if _span_overlaps_sorted(span, occupied):
                        continue

                    # Check `path:42`, `path:10-25`, `path:L42`, `path#L10-L20`
                    path_part, s_line, e_line = _split_path_and_line_suffix(trimmed)
                    if s_line is not None and _is_valid_file_path(path_part, has_line_spec=True):
                        _record(trimmed, path_part, s_line, e_line, sentence, span, occupied)
                        continue

                    # Check `line 42 of path/to/file.py` (bounded 60-char slice for O(n) runtime)
                    before_start = max(0, tok_start - 60)
                    before_text = sentence[before_start:tok_start]
                    m_line_of = _LINE_OF_PREFIX_RE.search(before_text)
                    if m_line_of and _is_valid_file_path(trimmed, has_line_spec=True):
                        bounds = _parse_line_bounds(
                            m_line_of.group("start"), m_line_of.group("end")
                        )
                        if bounds is not None:
                            full_span = (before_start + m_line_of.start(), tok_end)
                            if not _span_overlaps_sorted(full_span, occupied):
                                _record(
                                    sentence[full_span[0] : full_span[1]],
                                    trimmed,
                                    bounds[0],
                                    bounds[1],
                                    sentence,
                                    full_span,
                                    occupied,
                                )
                                continue

                    # Check `file.py (line 42)`, `file.py, line 42`, `file.py, L42`
                    after_text = sentence[tok_end : tok_end + 60]
                    m_after = _AFTER_LINE_RE.match(after_text)
                    if m_after and _is_valid_file_path(trimmed, has_line_spec=True):
                        bounds = _parse_line_bounds(
                            m_after.group("pstart") or m_after.group("astart"),
                            m_after.group("pend") or m_after.group("aend"),
                        )
                        if bounds is not None:
                            full_span = (tok_start, tok_end + m_after.end())
                            if not _span_overlaps_sorted(full_span, occupied):
                                _record(
                                    sentence[full_span[0] : full_span[1]],
                                    trimmed,
                                    bounds[0],
                                    bounds[1],
                                    sentence,
                                    full_span,
                                    occupied,
                                )

    for bc in bare_candidates:
        if bc.file_path not in files_with_line_claims:
            claims.append(bc)

    return claims


# --- Quoted Code Snippet Extraction ---

_SINGLE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_CLI_FLAG_RE = re.compile(r"^--?[A-Za-z0-9_=-]+$")
_CODE_SIGNAL_RE = re.compile(
    r"(?:"
    r"[A-Za-z_][A-Za-z0-9_.]*\([^)]+\)|"
    r"[A-Za-z_][A-Za-z0-9_.]*\(\)\s*\.|"
    r"\b(?:def|class|return|import|from|async|await|raise|yield|lambda|const|let|var|function|fn|pub|struct|impl|enum|match|if|elif|else|for|while|try|except|catch|throw|select|where)\b\s+\S|"
    r"[A-Za-z0-9_\])]\s*(?:==|!=|<=|>=|=>|->|:=|\+=|-=|\*=|/=|&&|\|\||=)\s*\S|"
    r"[A-Za-z_][A-Za-z0-9_]*\[[^\]]+\]"
    r")"
)
_SHELL_CMD_START_RE = re.compile(
    r"^(?:[A-Z_][A-Z0-9_]*=\S+\s+)*"
    r"(?:uv|git|pytest|ruff|python3?|pip|npm|npx|pnpm|yarn|bun|cargo|go|make|rg|grep|find|fd|"
    r"cat|head|tail|sed|awk|ls|cd|rm|mv|cp|mkdir|chmod|curl|wget|docker|kubectl|gh|tox|nox|"
    r"poetry|mypy|flake8|eslint|prettier|tsc|jest|vitest|playwright|alembic|handoff-verify)\b"
)

# B14: colon-terminated alternatives sit OUTSIDE `\b` so they match at end-of-line!
_SUGGESTION_OR_HYPOTHETICAL_QUOTE_RE = re.compile(
    r"(?:"
    r"\b(?:"
    r"for\s+example|e\.g\.|for\s+instance|such\s+as|hypothetically|"
    r"suggested(?:\s+fix)?|suggestion|propos(?:e|ed|ing)|recommend(?:ed|ing)?|"
    r"you\s+(?:can|could|should|may|might)|we\s+(?:can|could|should)|"
    r"try\s+(?:using|running|changing|replacing)|"
    r"consider\s+(?:using|changing|replacing|adding)|"
    r"should\s+(?:be|return|call|use|look\s+like)|"
    r"could\s+(?:be|return|call|use|look\s+like)|"
    r"would\s+(?:be|return|call|use|look\s+like)|"
    r"change\s+(?:it\s+|this\s+|that\s+)?to|chang(?:e|ing)\s+to|"
    r"replace\s+(?:it\s+|this\s+|that\s+)?with|instead\s+of|"
    r"to\s+fix(?:\s+this)?|fix\s+(?:would\s+be|is\s+to)|"
    r"add\s+the\s+following|snippet\s+to\s+add|sample\s+code|pseudocode|"
    r"run\s+this|after\s+applying|next\s+step|"
    r"add\s+to|insert\s+into|append\s+to"
    r")\b|"
    r"\b(?:usage|example|next\s+step)\s*:"
    r")",
    re.IGNORECASE,
)

_COMMAND_INVOCATION_PREFIX_RE = re.compile(
    r"\b(?:ran|run|running|execute|executed|executing|invoked|invoke|via|using|command)\s*[:\-]?\s*$",
    re.IGNORECASE,
)

# B7 + B14 + R2-6: past-tense output cues (avoid present-tense feature descriptions like
# "`cli.py` emits structured output:" matching example JSON schemas).
_OUTPUT_ATTRIBUTION_RE = re.compile(
    r"(?:"
    r"\b(?:stdout|stderr|printed|logged|emitted|"
    r"failed\s+with|error\s+message|traceback|reports|reported|produced|"
    r"output\s+(?:shows|is|contains|was))\b|"
    r"(?<!structured\s)(?<!example\s)(?<!sample\s)(?<!json\s)\b(?:output|stdout|stderr|logs|shows|returned|printed|traceback)\s*:|"
    r"\breturned\s+`"
    r")",
    re.IGNORECASE,
)

# B14: colon-terminated cues moved outside `\b`; also support "relevant code:"
_FENCE_FILE_ATTRIBUTION_RE = re.compile(
    r"(?:"
    r"\b(?:from\s+(?:the\s+)?file|in\s+(?:the\s+)?file|currently\s+(?:reads|contains|has)|"
    r"current\s+(?:implementation|code)|exact\s+(?:code|lines?|content|snippet)|"
    r"verbatim|excerpt\s+from|lines?\s+\d+|implementation\s+(?:in|from)|"
    r"relevant\s+code|existing\s+code)\b|"
    r"\b(?:contains|reads|code)\s*:"
    r")",
    re.IGNORECASE,
)

_CONTRAST_BETWEEN_RE = re.compile(
    r"\b(?:unlike|whereas|compared\s+to|rather\s+than|instead\s+of|new\s+module|"
    r"other\s+(?:file|module)|separate\s+(?:file|module))\b",
    re.IGNORECASE,
)

_FENCE_SKIP_LANGS: frozenset[str] = frozenset({"diff", "patch"})
_ELLIPSIS_PLACEHOLDER_RE = re.compile(r"(?:\.\.\.|<\w+>)")
_TYPE_ANNOTATION_RE = re.compile(
    r"^(?:dict|list|tuple|set|frozenset|Optional|Union|Callable|Sequence|Mapping|"
    r"Iterable|Iterator|Literal|Type|ClassVar|Final|Annotated|Array|Record|Promise|"
    r"Partial|Required|Readonly|Map|Set)\[[^\]]+\]$"
)
_SIMPLE_CALL_SHORTHAND_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*\([^()]*\)$")
_SIMPLE_KWARG_ONLY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*=(?:True|False|None|\d+|\"[^\"]*\"|'[^']*')$"
)


def _looks_like_diff_body(body: str) -> bool:
    """Return True if an untagged fenced block looks like a unified/inline diff."""
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    if any(ln.startswith(("@@ ", "--- ", "+++ ", "diff --git ")) for ln in lines[:5]):
        return True
    plus_lines = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("++"))
    minus_lines = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("--"))
    return plus_lines >= 1 and minus_lines >= 1 and (plus_lines + minus_lines) * 2 >= len(lines)


def _is_multi_token_code_snippet(snippet: str) -> bool:
    """Return True if an inline backticked string is a verifiable multi-token code snippet."""
    s = snippet.strip()
    if len(s) < 8 or len(s) > 400:
        return False
    if _SINGLE_IDENT_RE.fullmatch(s) or _CLI_FLAG_RE.fullmatch(s):
        return False
    if _TYPE_ANNOTATION_RE.fullmatch(s):
        return False
    if _is_valid_file_path(s, has_line_spec=False):
        return False
    if "://" in s:
        return False
    if _ELLIPSIS_PLACEHOLDER_RE.search(s):
        return False
    return bool(_CODE_SIGNAL_RE.search(s))


def _is_fenced_quote_candidate(
    block: _FencedBlock, *, base_dirs: Sequence[str] = ()
) -> tuple[bool, str | None, int | None, int | None]:
    """Determine if a fenced block is claimed verbatim from a file or command output."""
    if block.lang in _FENCE_SKIP_LANGS:
        return False, None, None, None
    body = block.body.strip()
    if not body or len(body) < 8:
        return False, None, None, None
    if _looks_like_diff_body(body):
        return False, None, None, None

    for line in body.splitlines():
        ls = line.strip()
        if ls in ("...", "# ...", "// ...", "/* ... */", "<!-- ... -->"):
            return False, None, None, None

    prev_lines = [ln.strip() for ln in block.preceding_text.splitlines() if ln.strip()][-2:]
    prev_context = " ".join(prev_lines)
    if not prev_context:
        return False, None, None, None

    if _SUGGESTION_OR_HYPOTHETICAL_QUOTE_RE.search(prev_context):
        return False, None, None, None

    has_output_attr = bool(_OUTPUT_ATTRIBUTION_RE.search(prev_context))
    has_file_attr = bool(_FENCE_FILE_ATTRIBUTION_RE.search(prev_context))

    # R2-6: Skip bash/shell CLI usage blocks unless explicitly introduced as command output
    if block.lang in ("sh", "bash", "zsh", "shell", "console") and not has_output_attr:
        first_nonempty = next(
            (ln.strip().lstrip("$ ") for ln in body.splitlines() if ln.strip()), ""
        )
        if _SHELL_CMD_START_RE.match(first_nonempty) or re.search(
            r"\b(?:run|execute|install|usage|command|flag|cli)\b", prev_context, re.IGNORECASE
        ):
            return False, None, None, None

    file_pointers = extract_pointer_claims(prev_context, base_dirs=base_dirs)
    if file_pointers:
        last_ptr = file_pointers[-1]
        # R2-6: Require either a line-numbered pointer OR explicit file/output attribution cue
        # so "`src/cli.py` now supports a `--json` flag:\n```bash\n..." isn't treated as a quote.
        if last_ptr.start_line is not None or has_file_attr or has_output_attr:
            return True, last_ptr.file_path, last_ptr.start_line, last_ptr.end_line
        return False, None, None, None

    if has_output_attr or has_file_attr:
        return True, None, None, None

    return False, None, None, None


def extract_quote_claims(
    text: str,
    *,
    message_index: int | None = None,
    base_dirs: Sequence[str] = (),
) -> list[Claim]:
    """Extract inline and fenced quoted code/output claims from assistant text."""
    prose_chunks, fences = _segment_markdown(text)
    claims: list[Claim] = []
    seen: set[tuple[str | None, str]] = set()

    for chunk in prose_chunks:
        for _, _, sentence in _iter_sentences(chunk.text):
            sentence_pointers = extract_pointer_claims(
                sentence, message_index=message_index, base_dirs=base_dirs
            )
            has_output_cue = bool(_OUTPUT_ATTRIBUTION_RE.search(sentence))
            if not sentence_pointers and not has_output_cue:
                continue

            pointer_texts = {p.text.strip("`") for p in sentence_pointers}
            pointer_paths = {p.file_path for p in sentence_pointers if p.file_path}

            for m_bt in _BACKTICK_SPAN_RE.finditer(sentence):
                snippet = m_bt.group(1).strip()
                if not snippet:
                    continue
                if (
                    snippet in pointer_texts
                    or normalize_path(snippet, base_dirs=base_dirs) in pointer_paths
                ):
                    continue
                # Skip if snippet itself is a path:line pointer
                cand_p, cand_s, _ = _split_path_and_line_suffix(snippet)
                if cand_s is not None and _is_valid_file_path(cand_p, has_line_spec=True):
                    continue

                # Skip shell command invocations (e.g. `npx tsc --noEmit`, `CI=1 pnpm test`)
                prefix = sentence[max(0, m_bt.start() - 80) : m_bt.start()]
                if _SHELL_CMD_START_RE.match(snippet) and (
                    _COMMAND_INVOCATION_PREFIX_RE.search(prefix)
                    or not sentence_pointers
                    or re.match(
                        r"^\s*(?:reports?|returned|output|exited|passed|failed|ran|shows?)\b",
                        sentence[m_bt.end() : m_bt.end() + 30],
                        re.IGNORECASE,
                    )
                ):
                    continue

                clause_prefix = prefix
                for sep in (";", " — ", ", "):
                    idx = clause_prefix.rfind(sep)
                    if (
                        idx != -1
                        and idx > len(clause_prefix) - 40
                        and not re.search(
                            r"(?:e\.g\.|for\s+example|instead)\s*$",
                            clause_prefix[:idx],
                            re.IGNORECASE,
                        )
                    ):
                        clause_prefix = clause_prefix[idx + len(sep) :]
                if _SUGGESTION_OR_HYPOTHETICAL_QUOTE_RE.search(clause_prefix):
                    continue

                # If output-only (no file pointers), require output cue to PRECEDE the snippet
                # so "`my_cmd` reports no errors" isn't treated as a quote of output.
                output_cue_before = bool(_OUTPUT_ATTRIBUTION_RE.search(sentence[: m_bt.start()]))

                is_code = _is_multi_token_code_snippet(snippet)
                is_output_str = (
                    output_cue_before
                    and len(snippet) >= 10
                    and not _SINGLE_IDENT_RE.fullmatch(snippet)
                    and not _CLI_FLAG_RE.fullmatch(snippet)
                    and not _SHELL_CMD_START_RE.match(snippet)
                    and not _ELLIPSIS_PLACEHOLDER_RE.search(snippet)
                    and (" " in snippet or ":" in snippet)
                )
                if not (is_code or is_output_str):
                    continue
                if not sentence_pointers and not output_cue_before:
                    continue

                assoc_file: str | None = None
                assoc_start: int | None = None
                assoc_end: int | None = None
                if sentence_pointers:
                    chosen = None
                    best_pos = -1
                    for ptr in sentence_pointers:
                        pos = sentence.find(ptr.text)
                        if pos != -1 and pos < m_bt.start() and pos >= best_pos:
                            best_pos = pos
                            chosen = ptr
                    if chosen is not None:
                        # B7: Do NOT bind to `chosen` if sentence contrasts against it
                        # ("Unlike `src/a.py:5`, the new module uses `await client.fetch(url)`")
                        between = sentence[max(0, best_pos - 15) : m_bt.start()]
                        if not _CONTRAST_BETWEEN_RE.search(between):
                            assoc_file = chosen.file_path
                            assoc_start = chosen.start_line
                            assoc_end = chosen.end_line
                    elif not output_cue_before:
                        # R2-5: Subject-position call/signature shorthand BEFORE the file pointer
                        # ("`validate_token(token)` in `src/auth.py:3` checks the token.") is a
                        # prose symbol reference, not a verbatim code quote.
                        if _SIMPLE_CALL_SHORTHAND_RE.fullmatch(
                            snippet
                        ) or _SIMPLE_KWARG_ONLY_RE.fullmatch(snippet):
                            continue
                        first_ptr = sentence_pointers[0]
                        ptr_pos = sentence.find(first_ptr.text)
                        between = sentence[m_bt.end() : ptr_pos] if ptr_pos != -1 else ""
                        if not _CONTRAST_BETWEEN_RE.search(between):
                            assoc_file = first_ptr.file_path
                            assoc_start = first_ptr.start_line
                            assoc_end = first_ptr.end_line
                # R2-5: Skip bare `flag=True` / `strict=True` unless introduced by `sets`/`assigns`
                if _SIMPLE_KWARG_ONLY_RE.fullmatch(snippet) and not re.search(
                    r"\b(?:sets?|assigns?|uses?|with|from|to)\s+$", prefix, re.IGNORECASE
                ):
                    continue

                key = (assoc_file, snippet)
                if key not in seen:
                    seen.add(key)
                    claims.append(
                        Claim(
                            kind="quote",
                            text=m_bt.group(0),
                            file_path=assoc_file,
                            start_line=assoc_start,
                            end_line=assoc_end,
                            snippet=snippet,
                            context=sentence,
                            message_index=message_index,
                        )
                    )

    for block in fences:
        ok, file_path, start_line, end_line = _is_fenced_quote_candidate(block, base_dirs=base_dirs)
        if not ok:
            continue
        norm_fp = normalize_path(file_path, base_dirs=base_dirs) if file_path else None
        snippet = block.body.strip()
        key = (norm_fp, snippet)
        if key not in seen:
            seen.add(key)
            prev_lines = [ln.strip() for ln in block.preceding_text.splitlines() if ln.strip()][-2:]
            claims.append(
                Claim(
                    kind="quote",
                    text=snippet[:120] + ("..." if len(snippet) > 120 else ""),
                    file_path=norm_fp,
                    start_line=start_line,
                    end_line=end_line,
                    snippet=snippet,
                    context=" ".join(prev_lines),
                    message_index=message_index,
                )
            )

    return claims


# --- Absence & Completeness Claim Extraction ---

# Strong codebase-absence patterns (always indicate a search/inspection absence claim)
_ABSENCE_STRONG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bno\s+(?:other\s+|remaining\s+|further\s+|additional\s+|more\s+)?"
        r"(?:callers?|usages|uses|occurrences?|hits|importers?|implementations?|definitions?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\s+(?:other|remaining|further|additional|more)\s+usage\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\s+usage\s+(?:of\b|found\b|"
        r"(?:in|under|within|across)\s+(?:(?:the\s+)?(?:codebase|repo|repository|project|module|file|dir|directory|package)\b|`|[A-Za-z0-9_@.~-]+/)|"
        r"anywhere\b|elsewhere\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bfound\s+no\s+usage\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\s+(?:other|remaining|further|additional|more)\s+references?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\s+references?\b(?!\s+to\s+(?:global|local|external|internal|shared|mutable)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:zero|0)\s+(?:other\s+|remaining\s+)?"
        r"(?:callers?|references?|usages?|matches|occurrences?|hits|results?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:not|never)\s+(?:used|called|referenced|imported|invoked|found|present|defined)\s+"
        r"(?:from\s+)?(?:anywhere(?:\s+else)?|elsewhere|in\s+(?:the\s+)?(?:codebase|repo|repository|project))\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bnowhere\s+else\b", re.IGNORECASE),
    re.compile(
        r"\bdoes\s+not\s+(?:appear|exist|occur)\s+"
        r"(?:anywhere(?:\s+else)?|elsewhere|in\s+(?:the\s+)?(?:codebase|repo|repository|project))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:isn't|is\s+not|aren't|are\s+not)\s+"
        r"(?:used|called|referenced|imported)\s+(?:from\s+)?(?:anywhere(?:\s+else)?|elsewhere)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bunused\s+(?:anywhere|across\s+(?:the\s+)?(?:codebase|repo|repository|project))\b",
        re.IGNORECASE,
    ),
    # NB4 phrasing gaps
    re.compile(
        r"\b(?:didn't|did\s+not)\s+find\s+any\s+(?:other\s+|remaining\s+|more\s+)?"
        r"(?:callers?|references?|usages?|matches|occurrences?|items?|results?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bnothing\s+else\s+(?:in\s+(?:the\s+)?(?:codebase|repo|repository|project)\s+)?"
        r"(?:uses|calls|references|imports)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bnone\s+of\s+(?:the\s+)?(?:(?:open|remaining|matching|active)\s+)?"
        r"(?:issues?|tickets?|prs?|pull\s+requests?)\s+"
        r"(?:mention(?:s|ed)?|contain(?:s|ed)?|reference(?:s|d)?|report(?:s|ed)?|"
        r"include(?:s|d)?|describe(?:s|d)?|match(?:es|ed)?|have|has)\b",
        re.IGNORECASE,
    ),
)

# Contextual absence patterns (require genuine search/pagination/scope framing — never self-match)
_ABSENCE_CONTEXTUAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bno\s+(?:other\s+|remaining\s+|further\s+|additional\s+|more\s+)?"
        r"(?:matches|results?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\s+(?:other|remaining|further|additional|more|matching)\s+"
        r"(?:issues?|items?|files?|prs?|pull\s+requests?|records?|pages?|tickets?|todos?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bno\s+(?:todos?|fixmes?)\b", re.IGNORECASE),
    re.compile(r"\bnone\s+found\b", re.IGNORECASE),
)

_NON_SEARCH_ABSENCE_AFTER_RE = re.compile(
    r"^\s*(?:"
    r"(?:were|was|have\s+been|had\s+been)\s+(?:modified|changed|edited|touched|deleted|added|created|updated|affected)|"
    r"(?:needed|need|required|require)\s+(?:changes?|edits?|updates?|modifications?)|"
    r"(?:with\s+this|with\s+the|on\s+the\s+checklist|in\s+the\s+log)|"
    r"(?:are|were|is)\s+(?:reused|shared|cached|stored|saved|kept)|"
    r"to\s+(?:change|update|modify|edit|fix)|"
    r"(?:limits?|quotas?|docs?|documentation|examples?|instructions?|guide|section|"
    r"notes?|text|help|data|stats?|statistics|metrics?|tracking|telemetry|billing|fees?|costs?)"
    r")\b",
    re.IGNORECASE,
)

_OPEN_LISTING_ABSENCE_RE = re.compile(
    r"\bno\s+open\s+(?:issues?|prs?|pull\s+requests?|tickets?)\b",
    re.IGNORECASE,
)
_EXPLICIT_LISTING_FRAME_RE = re.compile(
    r"\b(?:gh\s+issue|gh\s+pr|list(?:ed|ing)?|returned|pages?|cursors?|"
    r"reviewed\s+all|checked\s+all)\b",
    re.IGNORECASE,
)

# R2-4: Removed self-satisfying words ("found", "remaining", "there are", "exist", "prs", "tickets")
# so ordinary closing prose ("All tests pass and there are no open PRs", "No further issues found")
# is NOT mistaken for an unverified search/pagination absence claim.
_SEARCH_OR_LISTING_VERB_RE = re.compile(
    r"\b(?:search(?:ed|ing)?|grep(?:ped|ping)?|rg|list(?:ed|ing)?|"
    r"reviewed\s+all|checked\s+all|scanned|audited|returned|shows?|showed|matching|"
    r"repo|repository|codebase|project|anywhere|elsewhere|pages?|cursors?|"
    r"gh\s+issue|gh\s+pr)\b",
    re.IGNORECASE,
)

_COMPLETENESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:checked|reviewed|inspected|audited|scanned|examined|searched|read|fetched|"
        r"listed|analyzed|processed|went\s+through|iterated\s+(?:through|over))\s+"
        r"(?:all(?:\s+of\s+the)?|every(?:\s+single)?)\s+"
        r"(?:(?:open|remaining|matching|active|\d+)\s+)?"
        r"(?:files?|pages?|results?|items?|matches|callers?|references?|usages?|issues?|"
        r"tickets?|prs?|pull\s+requests?|records?|entries|occurrences?|modules?|packages?|"
        r"directories|folders?|endpoints?|handlers?|logs?|commits?|branches?|comments?)\b"
        r"(?!\s+(?:you\s+(?:listed|mentioned|named|asked|specified|provided)|requested|specified))",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:checked|reviewed|inspected|audited|scanned|examined|searched)\s+"
        r"(?:the\s+)?(?:entire|whole|full)\s+(?:codebase|repo|repository|project|workspace|tree)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:exhaustively|thoroughly|completely)\s+"
        r"(?:checked|searched|reviewed|inspected|scanned|audited)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bevery\s+(?:single\s+)?file\s+(?:in\s+(?:the\s+)?(?:repo|repository|codebase|project|directory|folder|`[^`]+`)|"
        r"(?:was|has\s+been)\s+(?:checked|reviewed|inspected|scanned|searched))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:all\s+(?:the\s+)?(?:pages|results|matching\s+files|occurrences|callers|references))\b"
        r"(?!\s+(?:pass|passed|passing|succeed|succeeded|fail|failed|failing|are\s+already))",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:checked|verified|reviewed)\s+all\s+(?:of\s+)?them\b",
        re.IGNORECASE,
    ),
)

_HEDGING_BEFORE_RE = re.compile(
    r"\b(?:"
    r"cannot|can't|couldn't|could\s+not|unable\s+to|did\s+not|didn't|have\s+not|haven't|"
    r"not\s+sure|unclear|might|may\s+(?:still\s+)?(?:be|have)|if\s+there\s+(?:are|were)|"
    r"without\s+(?:checking|searching|reviewing)|only\s+(?:checked|searched|reviewed)\s+(?:a\s+)?subset"
    r")\b",
    re.IGNORECASE,
)

# B9: post-match hedging ("..., but I only searched `src/auth/`, so treat this as partial.")
_HEDGING_AFTER_RE = re.compile(
    r"\b(?:"
    r"(?:but|however|though|although)\s+(?:I\s+)?only\s+(?:searched|checked|grepped|scanned|reviewed)|"
    r"treat\s+this\s+as\s+partial|partial\s+(?:search|check|result)|"
    r"(?:did\s+not|didn't|haven't|have\s+not)\s+(?:search|check|review|fetch|read|load)\s+"
    r"(?:the\s+)?(?:rest|other|remaining|next)|"
    r"may\s+(?:still\s+)?(?:exist|be)\s+elsewhere"
    r")\b",
    re.IGNORECASE,
)

# B11 + R2-3: Explicit repo-wide wording overrides incidental path mentions,
# EXCEPT when "anywhere/elsewhere" is directly qualified by a specific dir/file.
_EXPLICIT_REPO_WIDE_NOUN_RE = re.compile(
    r"\b(?:"
    r"(?:in|across)\s+(?:the\s+)?(?:entire\s+|whole\s+|full\s+)?(?:repo|repository|codebase|project|workspace)|"
    r"(?:entire|whole|full)\s+(?:repo|repository|codebase|project|workspace)"
    r")\b",
    re.IGNORECASE,
)
_ANYWHERE_RE = re.compile(r"\b(?:anywhere(?:\s+else)?|elsewhere|nowhere\s+else)\b", re.IGNORECASE)
_SCOPED_AFTER_ANYWHERE_RE = re.compile(
    r"^\s+(?:in|under|within|inside|across)\s+(?:the\s+)?"
    r"(?!(?:entire\s+|whole\s+|full\s+)?(?:repo|repository|codebase|project|workspace)\b)"
    r"(?:`[^`\n]+`|(?:[A-Za-z0-9_@.~-]+/)+[A-Za-z0-9_@.~-]*|[A-Za-z0-9_@.~-]+\.[A-Za-z0-9]+|"
    r"[A-Za-z0-9_.-]+\s+(?:directory|dir|folder|package|module|file))",
    re.IGNORECASE,
)

_REPO_WIDE_NOUNS: frozenset[str] = frozenset(
    {
        "repo",
        "repository",
        "codebase",
        "project",
        "workspace",
        "tree",
        "code",
        "entire",
        "whole",
        "full",
        "the",
    }
)

_COMMON_TOP_LEVEL_DIRS: frozenset[str] = frozenset(
    {
        "src",
        "lib",
        "app",
        "apps",
        "pkg",
        "cmd",
        "tests",
        "test",
        "spec",
        "specs",
        "docs",
        "doc",
        "scripts",
        "tools",
        "internal",
        "packages",
        "services",
        "modules",
        "components",
        "utils",
        "core",
        "api",
        "web",
        "server",
        "client",
        "backend",
        "frontend",
        "config",
        "configs",
        "evals",
        "fixtures",
        "vendor",
        "legacy",
        "migrations",
        "bin",
        "include",
        "examples",
    }
)

_SCOPE_QUALIFIER_BACKTICK_RE = re.compile(
    r"\b(?:in|under|within|inside|across)\s+`([^`\n]{1,200})`",
    re.IGNORECASE,
)

# B9: "searched `src/auth/`", "grepped `src/auth`", "grep of src/auth", "search of `src/auth`"
_SCOPE_QUALIFIER_SEARCH_VERB_RE = re.compile(
    r"\b(?:"
    r"(?:searched|grepped|scanned)\s+(?:only\s+)?(?:in\s+|under\s+)?|"
    r"(?:grep|search|scan)\s+(?:of|in|under|on)\s+"
    r")(?:the\s+)?`?(?P<path>[A-Za-z0-9_@.+~/-]{1,200})`?",
    re.IGNORECASE,
)

_SCOPE_QUALIFIER_BARE_PATH_RE = re.compile(
    r"\b(?:in|under|within|inside|across)\s+(?:the\s+)?"
    r"(?P<path>(?:[A-Za-z0-9_@.~-]+/)+[A-Za-z0-9_@.~-]*|[A-Za-z0-9_@.~-]+\.[A-Za-z0-9]+)"
    r"(?:\s+(?:directory|dir|folder|package|module|file))?",
    re.IGNORECASE,
)

_SCOPE_QUALIFIER_NAMED_DIR_RE = re.compile(
    r"\b(?:in|under|within|inside)\s+(?:the\s+)?`?(?P<dir>[A-Za-z0-9_./-]+)`?\s+"
    r"(?:directory|dir|folder|subdir|subdirectory|package|module)\b",
    re.IGNORECASE,
)

_EXCEPTION_BEFORE_SCOPE_RE = re.compile(
    r"\b(?:besides|except|other\s+than|apart\s+from|only\s+(?:called|used|found|defined|present))\b",
    re.IGNORECASE,
)


def _is_plausible_scope_path(candidate: str, *, allow_any_dir_name: bool = False) -> bool:
    raw = candidate.strip().strip("`\"'")
    had_trailing_slash = raw.endswith("/")
    s = raw.rstrip("/")
    if not s or len(s) > 200:
        return False
    if any(ch in s for ch in (" ", "\t", "\n", "*", "?", "<", ">", "(", ")", "{", "}", "=")):
        return False
    if "://" in s:
        return False
    if s.lower() in _REPO_WIDE_NOUNS:
        return False
    if "/" in s or had_trailing_slash:
        return True
    if _is_valid_file_path(s, has_line_spec=False):
        return True
    if allow_any_dir_name and re.fullmatch(r"[A-Za-z0-9_.-]+", s) and not s.isdigit():
        return True
    return s.lower() in _COMMON_TOP_LEVEL_DIRS


def has_explicit_repo_wide_wording(sentence: str) -> bool:
    """Return True if the sentence explicitly claims repo-wide/codebase-wide coverage."""
    if _EXPLICIT_REPO_WIDE_NOUN_RE.search(sentence):
        return True
    for m in _ANYWHERE_RE.finditer(sentence):
        after = sentence[m.end() : m.end() + 80]
        if not _SCOPED_AFTER_ANYWHERE_RE.match(after):
            return True
    return False


def extract_scope_qualifiers(
    sentence: str,
    *,
    base_dirs: Sequence[str] = (),
) -> list[str]:
    """Extract explicit directory/file scope qualifiers from a sentence (empty if repo-wide)."""
    # B11: explicit repo-wide wording ("anywhere in the repo besides the one in `src/a.py`")
    # overrides incidental path mentions unless the sentence also hedges to a single searched dir.
    if has_explicit_repo_wide_wording(sentence):
        return []

    qualifiers: list[str] = []
    seen: set[str] = set()

    def _add(raw: str, match_start: int, *, allow_any_dir_name: bool = False) -> None:
        # Ignore exception clauses ("besides the one in `src/a.py`")
        pre = sentence[max(0, match_start - 40) : match_start]
        if _EXCEPTION_BEFORE_SCOPE_RE.search(pre):
            return
        if not _is_plausible_scope_path(raw, allow_any_dir_name=allow_any_dir_name):
            return
        norm = normalize_path(raw.rstrip("/"), base_dirs=base_dirs)
        if norm and norm not in (".", "./") and norm not in seen:
            seen.add(norm)
            qualifiers.append(norm)

    for m in _SCOPE_QUALIFIER_BACKTICK_RE.finditer(sentence):
        raw = m.group(1).strip()
        raw_clean = re.sub(r"(?::L?\d+(?:-\d+)?|#L\d+(?:-L?\d+)?)$", "", raw)
        _add(raw_clean, m.start())

    for m in _SCOPE_QUALIFIER_SEARCH_VERB_RE.finditer(sentence):
        raw = m.group("path").strip().rstrip(".,;:")
        if raw.lower() in _REPO_WIDE_NOUNS:
            continue
        _add(raw, m.start())

    for m in _SCOPE_QUALIFIER_BARE_PATH_RE.finditer(sentence):
        raw = m.group("path").strip().rstrip(".,;:")
        if raw.lower() in _REPO_WIDE_NOUNS:
            continue
        _add(raw, m.start())

    for m in _SCOPE_QUALIFIER_NAMED_DIR_RE.finditer(sentence):
        raw = m.group("dir").strip()
        if raw.lower() in _REPO_WIDE_NOUNS:
            continue
        _add(raw, m.start(), allow_any_dir_name=True)

    if not qualifiers and re.search(r"\b(?:in|within)\s+this\s+file\b", sentence, re.IGNORECASE):
        ptrs = extract_pointer_claims(sentence, base_dirs=base_dirs)
        if ptrs and ptrs[0].file_path:
            _add(ptrs[0].file_path, 0)

    return qualifiers


def _has_hedging(sentence: str, match_start: int, match_end: int) -> bool:
    prefix = sentence[max(0, match_start - 70) : match_start]
    for sep in (";", " — ", ". ", ", "):
        idx = prefix.rfind(sep)
        if idx != -1:
            prefix = prefix[idx + len(sep) :]
    if _HEDGING_BEFORE_RE.search(prefix):
        return True
    suffix = sentence[match_end : min(len(sentence), match_end + 120)]
    return bool(_HEDGING_AFTER_RE.search(suffix))


_ANAPHORIC_SCOPE_RE = re.compile(
    r"\b(?:"
    r"(?:found|present|exist(?:s|ed)?|appear(?:s|ed)?|occur(?:s|red)?|"
    r"used|called|referenced|imported|defined|located|seen|matched)\s+there\b|"
    r"(?:in|under|within)\s+that\s+(?:dir|directory|folder|package|module|subdir|subdirectory|path)\b|"
    r"(?:in|under|within)\s+those\s+(?:files|dirs|directories|folders|packages|modules)\b"
    r")",
    re.IGNORECASE,
)
_THIS_FILE_REF_RE = re.compile(r"\b(?:in|within)\s+this\s+file\b", re.IGNORECASE)


def extract_scope_claims(
    text: str,
    *,
    message_index: int | None = None,
    base_dirs: Sequence[str] = (),
) -> list[Claim]:
    """Extract absence and completeness claims from assistant prose."""
    prose_chunks, _ = _segment_markdown(text)
    claims: list[Claim] = []
    seen: set[tuple[str, str]] = set()
    last_file_path: str | None = None

    for chunk in prose_chunks:
        prev_sentence_qualifiers: list[str] = []
        for _, _, sentence in _iter_sentences(chunk.text):
            sent_ptrs = extract_pointer_claims(sentence, base_dirs=base_dirs)
            for ptr in sent_ptrs:
                if ptr.file_path:
                    last_file_path = ptr.file_path

            raw_qualifiers = extract_scope_qualifiers(sentence, base_dirs=base_dirs)
            if raw_qualifiers:
                qualifiers = raw_qualifiers
            elif not has_explicit_repo_wide_wording(sentence):
                if _THIS_FILE_REF_RE.search(sentence) and last_file_path:
                    qualifiers = [last_file_path]
                elif _ANAPHORIC_SCOPE_RE.search(sentence) and prev_sentence_qualifiers:
                    qualifiers = list(prev_sentence_qualifiers)
                else:
                    qualifiers = []
            else:
                qualifiers = []
            prev_sentence_qualifiers = list(raw_qualifiers)
            primary_scope = qualifiers[0] if qualifiers else None

            # Absence claims: strong patterns first, then contextual patterns
            matched_absence = False
            for pat in _ABSENCE_STRONG_PATTERNS:
                m = pat.search(sentence)
                if not m:
                    continue
                if _has_hedging(sentence, m.start(), m.end()):
                    continue
                after = sentence[m.end() : m.end() + 45]
                if _NON_SEARCH_ABSENCE_AFTER_RE.match(after):
                    continue
                key = ("absence", sentence)
                if key not in seen:
                    seen.add(key)
                    claims.append(
                        Claim(
                            kind="absence",
                            text=m.group(0).strip(),
                            file_path=primary_scope,
                            context=sentence,
                            message_index=message_index,
                        )
                    )
                matched_absence = True
                break

            if not matched_absence:
                for pat in _ABSENCE_CONTEXTUAL_PATTERNS:
                    m = pat.search(sentence)
                    if not m:
                        continue
                    if _has_hedging(sentence, m.start(), m.end()):
                        continue
                    after = sentence[m.end() : m.end() + 45]
                    if _NON_SEARCH_ABSENCE_AFTER_RE.match(after):
                        continue
                    # B8 + R2-4: contextual patterns require search/listing/pagination framing
                    # OR an explicit file/directory scope qualifier ("no TODOs in `src/a.py`").
                    if not (qualifiers or _SEARCH_OR_LISTING_VERB_RE.search(sentence)):
                        continue
                    key = ("absence", sentence)
                    if key not in seen:
                        seen.add(key)
                        claims.append(
                            Claim(
                                kind="absence",
                                text=m.group(0).strip(),
                                file_path=primary_scope,
                                context=sentence,
                                message_index=message_index,
                            )
                        )
                    break
                else:
                    # "no open PRs/issues" only counts as an absence claim when explicitly
                    # framed by a listing/pagination tool or verb ("`gh pr list` returned
                    # no open PRs"), never in status prose ("no open issue blocking this PR").
                    m_open = _OPEN_LISTING_ABSENCE_RE.search(sentence)
                    if (
                        m_open
                        and not _has_hedging(sentence, m_open.start(), m_open.end())
                        and not _NON_SEARCH_ABSENCE_AFTER_RE.match(
                            sentence[m_open.end() : m_open.end() + 45]
                        )
                        and _EXPLICIT_LISTING_FRAME_RE.search(sentence)
                    ):
                        key = ("absence", sentence)
                        if key not in seen:
                            seen.add(key)
                            claims.append(
                                Claim(
                                    kind="absence",
                                    text=m_open.group(0).strip(),
                                    file_path=primary_scope,
                                    context=sentence,
                                    message_index=message_index,
                                )
                            )

            # Completeness claims
            for pat in _COMPLETENESS_PATTERNS:
                m = pat.search(sentence)
                if not m:
                    continue
                if _has_hedging(sentence, m.start(), m.end()):
                    continue
                key = ("completeness", sentence)
                if key not in seen:
                    seen.add(key)
                    claims.append(
                        Claim(
                            kind="completeness",
                            text=m.group(0).strip(),
                            file_path=primary_scope,
                            context=sentence,
                            message_index=message_index,
                        )
                    )
                break

    return claims


def extract_claims(
    transcript: ParsedTranscript,
    *,
    repo_root: Path | None = None,
) -> list[Claim]:
    """Extract all verifiable claims from the final assistant turn(s) of a transcript."""
    base_dirs = build_base_dirs(transcript.cwd, repo_root)
    messages = _select_target_messages(transcript)
    claims: list[Claim] = []

    for msg in messages:
        if not msg.text or not msg.text.strip():
            continue
        claims.extend(
            extract_pointer_claims(msg.text, message_index=msg.index, base_dirs=base_dirs)
        )
        claims.extend(extract_quote_claims(msg.text, message_index=msg.index, base_dirs=base_dirs))
        claims.extend(extract_scope_claims(msg.text, message_index=msg.index, base_dirs=base_dirs))

    return claims

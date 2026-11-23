"""Deterministic claim extraction from assistant messages.

Extracts structured `Claim` objects for:
- File pointers (`kind="pointer"`): path:line, path:start-end, path#L10-L20,
  path (line 42), line 42 of path, and backticked repo file paths.
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
from handoff_verify.verifiers._paths import build_base_dirs, normalize_path

# Comprehensive set of known source, config, doc, and data file extensions.
# Restricting bare/unqualified matches to known extensions (or known extensionless
# filenames) prevents false positives on dotted identifiers (`os.path`, `self.value`),
# versions (`3.11`), hostnames (`example.com`), and timestamps (`12:34`).
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
        ".config",
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

KNOWN_EXTENSIONLESS_FILES: frozenset[str] = frozenset(
    {
        "Makefile",
        "GNUmakefile",
        "Dockerfile",
        "Containerfile",
        "Procfile",
        "Gemfile",
        "Rakefile",
        "Justfile",
        "Vagrantfile",
        "Jenkinsfile",
        "Brewfile",
        "LICENSE",
        "README",
        "CHANGELOG",
        "AUTHORS",
        "CODEOWNERS",
        "BUILD",
        "WORKSPACE",
        "Tiltfile",
        "Earthfile",
        ".gitignore",
        ".dockerignore",
        ".editorconfig",
        ".npmrc",
        ".nvmrc",
        ".eslintrc",
        ".prettierrc",
        ".babelrc",
        ".env",
        ".python-version",
        ".tool-versions",
    }
)

# Bare dotted names that look like filenames with known extensions but are usually
# framework/technology names or non-file terms when mentioned without a path or line number.
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
        "e.g",
        "i.e",
    }
)

_MAX_LINE_NUMBER = 1_000_000

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
                # Backtick fences cannot contain backticks in their info string per CommonMark
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
            if (
                len(close_candidate) >= fence_len
                and set(close_candidate.rstrip(" \t")) == {fence_char}
                and len(close_candidate.rstrip(" \t")) >= fence_len
            ):
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
        # Unclosed fence: treat its opening text as prose rather than silently dropping
        if fence_start > prose_start:
            prose.append(_ProseChunk(prose_start, fence_start, text[prose_start:fence_start]))
    elif pos > prose_start:
        prose.append(_ProseChunk(prose_start, pos, text[prose_start:pos]))

    return prose, fences


# --- Helper: target assistant messages ---


def _select_target_messages(transcript: ParsedTranscript) -> list[AssistantMessage]:
    """Return final-turn assistant messages (after the last tool call) to verify."""
    if transcript.assistant_messages:
        last_msg = transcript.assistant_messages[-1]
        last_after = last_msg.after_event_index
        # Include contiguous trailing messages emitted after the same last tool call
        selected: list[AssistantMessage] = []
        for msg in reversed(transcript.assistant_messages):
            if msg.after_event_index == last_after or msg.is_final:
                selected.append(msg)
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
_PATH_TOKEN_CHARS = r"[A-Za-z0-9_@.+~/-]"


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
    }
)


def _is_valid_file_path(
    candidate: str,
    *,
    has_line_spec: bool = False,
) -> bool:
    """Return True if candidate looks like a genuine repo/source file path."""
    s = candidate.strip().strip("`\"'")
    if not s or len(s) > 300:
        return False
    if any(ch in s for ch in ("*", "?", "<", ">", "{", "}", "$", "|", ";", " ", "\t", "\n")):
        return False
    if "://" in s or s.startswith(("http:", "https:", "mailto:", "git@")):
        return False
    if s.startswith("-") or s.endswith("/"):
        return False
    if ".." in s.split("/"):
        # Allow relative `../foo.py` only if there's an actual filename after `..`
        parts = [p for p in s.split("/") if p and p != ".."]
        if not parts:
            return False

    norm = s.replace("\\", "/")
    base = posixpath.basename(norm)
    if not base or base in (".", ".."):
        return False

    # Reject bare host:port-like or IP-like strings (`127.0.0.1`, `example.com`)
    if "/" not in norm and base.lower() in _NON_FILE_BARE_NAMES:
        return False
    if all(ch.isdigit() or ch == "." for ch in base):
        return False

    # Known extensionless filenames (`Makefile`, `Dockerfile`, `.gitignore`, etc.)
    if base in KNOWN_EXTENSIONLESS_FILES:
        return True

    # Check extension
    _, ext = posixpath.splitext(base)
    if ext.lower() in KNOWN_FILE_EXTENSIONS:
        # Guard against bare version-like strings or single-letter/domain tokens
        stem = base[: -len(ext)]
        if not stem or stem == ".":
            # Dotfiles like `.env` are already in KNOWN_EXTENSIONLESS_FILES
            return base.lower() in KNOWN_EXTENSIONLESS_FILES
        # Reject bare dotted attribute accesses like `self.config`, `ctx.log`, `res.json`
        if "/" not in norm and not has_line_spec:
            if not any(c.isalpha() for c in stem):
                return False
            if stem.lower() in _CODE_RECEIVER_STEMS:
                return False
        return True

    # Multi-extension dotfiles/configs like `.env.example`
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
    r"please|consider|recommend|suggest|try|next\s+step\s*:?|todo\s*:?)\s+"
    r"(?:to\s+)?(?:create|creating|add|adding|write|writing|open|opening|edit|editing|make|making)\s+(?:a\s+(?:new\s+)?)?|"
    r"(?:should|could|would|will|to)\s+(?:create|add|write)\s+(?:a\s+(?:new\s+)?)?|"
    r"(?:^|\b)(?:create|add)\s+(?:a\s+new\s+file\s+|the\s+file\s+)?|"
    r"new\s+file\s+at\s+"
    r")$",
    re.IGNORECASE,
)

_NONEXISTENT_AROUND_RE = re.compile(
    r"(?:"
    r"\b(?:does\s+not\s+exist|doesn't\s+exist|did\s+not\s+exist|didn't\s+exist|"
    r"not\s+found|no\s+longer\s+exists?|is\s+missing|was\s+missing|was\s+not\s+found|"
    r"was\s+never\s+created|cannot\s+find|could\s+not\s+find|couldn't\s+find)\b"
    r")",
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
    """Return True if a pointer match sits in an example, prospective, or non-existence clause."""
    # Look at the immediate clause before the match (reset at sentence/semicolon/period boundaries)
    prefix = sentence[max(0, match_start - 90) : match_start]
    # Trim prefix to the current clause after `;` or `—`
    for sep in (";", " — ", "\n"):
        idx = prefix.rfind(sep)
        if idx != -1:
            prefix = prefix[idx + len(sep) :]

    # Immediate example cue right before match (or inside a short parenthetical)
    if _EXAMPLE_PREFIX_RE.search(prefix):
        return True
    # Check if inside a parenthetical starting with e.g. / for example / such as
    last_open = prefix.rfind("(")
    last_close = prefix.rfind(")")
    if last_open != -1 and last_open > last_close:
        paren_prefix = prefix[last_open + 1 :]
        if _EXAMPLE_CLAUSE_RE.search(paren_prefix):
            return True
    # Check if an example phrase appears within ~30 chars before the match in the same clause
    short_prefix = prefix[-35:]
    if _EXAMPLE_CLAUSE_RE.search(short_prefix):
        return True

    # CLI flag argument (e.g. `pass --config foo.yaml` or `--repo path/to/file`)
    if _COMMAND_FLAG_PREFIX_RE.search(prefix):
        return True

    # Prospective creation (`create new_file.py`), excluding past tense ("I created foo.py")
    if _PROSPECTIVE_PREFIX_RE.search(prefix) and not re.search(
        r"\b(?:I|we|has|have|was|were)\s+(?:created|added|wrote)\s+$", prefix, re.IGNORECASE
    ):
        return True

    # Non-existence report for line-less file mention (`src/missing.py does not exist`)
    if not has_line:
        suffix = sentence[match_end : min(len(sentence), match_end + 50)]
        near_prefix = prefix[-40:]
        if _NONEXISTENT_AROUND_RE.search(suffix) or re.search(
            r"\b(?:no|missing|without|cannot\s+find|could\s+not\s+find|couldn't\s+find)\s+$",
            near_prefix,
            re.IGNORECASE,
        ):
            return True

    return False


# --- File Pointer Patterns ---

# 1. Markdown links: `[label](target)` — handled first so we extract at most ONE claim per link
# 2. `path/to/file.py:10-25`, `path/to/file.py:42`, `path/to/file.py:42:10`
_COLON_LINE_RE = re.compile(
    r"(?P<bt>`?)"
    r"(?P<path>(?:[A-Za-z0-9_@.~-]+/)*[A-Za-z0-9_@.~-]+\.[A-Za-z0-9]+|"
    r"(?:[A-Za-z0-9_@.~-]+/)*(?:Makefile|Dockerfile|Containerfile|Procfile|Gemfile|Rakefile|Justfile|BUILD|WORKSPACE))"
    r"(?P=bt)"
    r":(?P<start>\d{1,6})(?:(?:\s*[-–]\s*|\.\.)(?P<end>\d{1,6})|:(?P<col>\d{1,6}))?"
    r"(?!\d|:\d)"
)

# 3. `path/to/file.py#L10`, `path/to/file.py#L10-L20`, `path/to/file.py#L10-20`
_HASH_LINE_RE = re.compile(
    r"(?P<bt>`?)"
    r"(?P<path>(?:[A-Za-z0-9_@.~-]+/)*[A-Za-z0-9_@.~-]+\.[A-Za-z0-9]+|"
    r"(?:[A-Za-z0-9_@.~-]+/)*(?:Makefile|Dockerfile|Containerfile|Procfile|Gemfile|Rakefile|Justfile|BUILD|WORKSPACE))"
    r"#L(?P<start>\d{1,6})(?:[-–]L?(?P<end>\d{1,6}))?"
    r"(?P=bt)"
)

# 4. `file.py (line 42)`, `file.py (lines 10-25)`, `file.py, line 42`, `file.py at line 42`
_PAREN_OR_AFTER_LINE_RE = re.compile(
    r"(?P<bt>`?)"
    r"(?P<path>(?:[A-Za-z0-9_@.~-]+/)*[A-Za-z0-9_@.~-]+\.[A-Za-z0-9]+|"
    r"(?:[A-Za-z0-9_@.~-]+/)*(?:Makefile|Dockerfile|Containerfile|Procfile|Gemfile|Rakefile|Justfile|BUILD|WORKSPACE))"
    r"(?P=bt)"
    r"(?:\s*\(\s*(?:lines?|L)\s*(?P<pstart>\d{1,6})(?:\s*[-–]\s*L?(?P<pend>\d{1,6}))?\s*\)"
    r"|(?:\s*,\s*|\s+(?:at\s+)?)(?:lines?)\s+(?P<astart>\d{1,6})(?:\s*[-–]\s*(?P<aend>\d{1,6}))?(?!\s+(?:of|in)\b))",
    re.IGNORECASE,
)

# 5. `line 42 of path/to/file.py`, `lines 10-25 in \`path/to/file.py\``
_LINE_OF_FILE_RE = re.compile(
    r"\blines?\s+(?P<start>\d{1,6})(?:\s*[-–]\s*(?P<end>\d{1,6}))?\s+"
    r"(?:of|in)\s+(?P<bt>`?)"
    r"(?P<path>(?:[A-Za-z0-9_@.~-]+/)*[A-Za-z0-9_@.~-]+\.[A-Za-z0-9]+|"
    r"(?:[A-Za-z0-9_@.~-]+/)*(?:Makefile|Dockerfile|Containerfile|Procfile|Gemfile|Rakefile|Justfile|BUILD|WORKSPACE))"
    r"(?P=bt)",
    re.IGNORECASE,
)

# 6. Backticked file path without line numbers: `` `src/auth/login.py` ``
_BACKTICK_PATH_RE = re.compile(r"`([^`\n]{1,300})`")


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


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
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

    # Strip file:// scheme if present
    if target_clean.lower().startswith("file://"):
        target_clean = target_clean[7:]

    # Parse target path + line spec
    t_path: str | None = None
    t_start: int | None = None
    t_end: int | None = None

    m_hash = _HASH_LINE_RE.fullmatch(target_clean)
    if m_hash:
        bounds = _parse_line_bounds(m_hash.group("start"), m_hash.group("end"))
        if bounds:
            t_path, t_start, t_end = m_hash.group("path"), bounds[0], bounds[1]
    else:
        m_colon = _COLON_LINE_RE.fullmatch(target_clean)
        if m_colon:
            bounds = _parse_line_bounds(m_colon.group("start"), m_colon.group("end"))
            if bounds:
                t_path, t_start, t_end = m_colon.group("path"), bounds[0], bounds[1]
        else:
            # Strip non-line anchor like `#section`
            bare_target = target_clean.split("#", 1)[0]
            if _is_valid_file_path(bare_target):
                t_path = bare_target

    # Also check label in case label has line numbers target lacks
    l_path: str | None = None
    l_start: int | None = None
    l_end: int | None = None
    label_stripped = label.strip().strip("`")
    for pat in (_HASH_LINE_RE, _COLON_LINE_RE):
        ml = pat.fullmatch(label_stripped)
        if ml:
            bounds = _parse_line_bounds(ml.group("start"), ml.group("end"))
            if bounds:
                l_path, l_start, l_end = ml.group("path"), bounds[0], bounds[1]
                break
    if l_path is None and _is_valid_file_path(label_stripped):
        l_path = label_stripped

    final_path = t_path or l_path
    if not final_path or not _is_valid_file_path(
        final_path, has_line_spec=bool(t_start or l_start)
    ):
        return None

    start = t_start if t_start is not None else l_start
    end = t_end if t_end is not None else l_end
    return normalize_path(final_path, base_dirs=base_dirs), start, end


def extract_pointer_claims(
    text: str,
    *,
    message_index: int | None = None,
    base_dirs: Sequence[str] = (),
) -> list[Claim]:
    """Extract file pointer claims from assistant prose (ignoring fenced code blocks)."""
    prose_chunks, _ = _segment_markdown(text)
    claims: list[Claim] = []
    seen_exact: set[tuple[str, int | None, int | None]] = set()
    files_with_line_claims: set[str] = set()
    bare_candidates: list[Claim] = []

    for chunk in prose_chunks:
        for _, _, sentence in _iter_sentences(chunk.text):
            occupied: list[tuple[int, int]] = []

            # 0. Mask external URLs and process markdown links first
            for m_link in _MARKDOWN_LINK_RE.finditer(sentence):
                span = (m_link.start(), m_link.end())
                occupied.append(span)
                parsed = _parse_pointer_from_link(
                    m_link.group(1), m_link.group(2), base_dirs=base_dirs
                )
                if parsed is None:
                    continue
                norm_p, s_line, e_line = parsed
                if _is_hypothetical_pointer(
                    sentence, m_link.start(), m_link.end(), has_line=s_line is not None
                ):
                    continue
                key = (norm_p, s_line, e_line)
                if key not in seen_exact:
                    seen_exact.add(key)
                    c = Claim(
                        kind="pointer",
                        text=m_link.group(0),
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

            for m_url in _URL_RE.finditer(sentence):
                occupied.append((m_url.start(), m_url.end()))

            # 1-4. Explicit line-numbered pointer patterns
            for pat in (_HASH_LINE_RE, _COLON_LINE_RE, _LINE_OF_FILE_RE, _PAREN_OR_AFTER_LINE_RE):
                for m in pat.finditer(sentence):
                    span = (m.start(), m.end())
                    if _overlaps(span, occupied):
                        continue
                    raw_path = m.group("path")
                    # Preserve leading `/` for bare absolute paths (not `//`)
                    if (
                        m.start() > 0
                        and sentence[m.start() - 1] == "/"
                        and (m.start() < 2 or sentence[m.start() - 2] != "/")
                    ):
                        raw_path = "/" + raw_path
                        span = (m.start() - 1, m.end())
                    if not _is_valid_file_path(raw_path, has_line_spec=True):
                        continue

                    if pat is _PAREN_OR_AFTER_LINE_RE:
                        s_raw = m.group("pstart") or m.group("astart")
                        e_raw = m.group("pend") or m.group("aend")
                    else:
                        s_raw = m.group("start")
                        e_raw = m.group("end")

                    bounds = _parse_line_bounds(s_raw, e_raw)
                    if bounds is None:
                        continue
                    if _is_hypothetical_pointer(sentence, span[0], span[1], has_line=True):
                        occupied.append(span)
                        continue

                    norm_p = normalize_path(raw_path, base_dirs=base_dirs)
                    occupied.append(span)
                    key = (norm_p, bounds[0], bounds[1])
                    if key not in seen_exact:
                        seen_exact.add(key)
                        files_with_line_claims.add(norm_p)
                        claims.append(
                            Claim(
                                kind="pointer",
                                text=m.group(0),
                                file_path=norm_p,
                                start_line=bounds[0],
                                end_line=bounds[1],
                                context=sentence,
                                message_index=message_index,
                            )
                        )

            # 5. Backticked file paths without line numbers (e.g. `` `src/auth/login.py` ``)
            for m_bt in _BACKTICK_PATH_RE.finditer(sentence):
                span = (m_bt.start(), m_bt.end())
                if _overlaps(span, occupied):
                    continue
                inner = m_bt.group(1).strip()
                if not _is_valid_file_path(inner, has_line_spec=False):
                    continue
                if _is_hypothetical_pointer(sentence, span[0], span[1], has_line=False):
                    occupied.append(span)
                    continue
                norm_p = normalize_path(inner, base_dirs=base_dirs)
                occupied.append(span)
                key = (norm_p, None, None)
                if key not in seen_exact:
                    seen_exact.add(key)
                    bare_candidates.append(
                        Claim(
                            kind="pointer",
                            text=m_bt.group(0),
                            file_path=norm_p,
                            start_line=None,
                            end_line=None,
                            context=sentence,
                            message_index=message_index,
                        )
                    )

    # Keep bare file-path claims only if that file wasn't already cited with specific lines
    for bc in bare_candidates:
        if bc.file_path not in files_with_line_claims:
            claims.append(bc)

    return claims


# --- Quoted Code Snippet Extraction ---

_SINGLE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_CLI_FLAG_RE = re.compile(r"^--?[A-Za-z0-9_=-]+$")
_CODE_SIGNAL_RE = re.compile(
    r"(?:"
    r"[A-Za-z_][A-Za-z0-9_.]*\([^)]+\)|"  # function/method call with args
    r"[A-Za-z_][A-Za-z0-9_.]*\(\)\s*\.|"  # chained call
    r"\b(?:def|class|return|import|from|async|await|raise|yield|lambda|const|let|var|function|fn|pub|struct|impl|enum|match|if|elif|else|for|while|try|except|catch|throw|select|from|where)\b\s+\S|"
    r"[A-Za-z0-9_\])]\s*(?:==|!=|<=|>=|=>|->|:=|\+=|-=|\*=|/=|&&|\|\||=)\s*\S|"
    r"[A-Za-z_][A-Za-z0-9_]*\[[^\]]+\]"  # subscript access
    r")"
)
_SHELL_CMD_START_RE = re.compile(
    r"^(?:uv|git|pytest|ruff|python3?|pip|npm|pnpm|yarn|bun|cargo|go|make|rg|grep|find|fd|cat|head|tail|sed|awk|ls|cd|rm|mv|cp|mkdir|chmod|curl|wget|docker|kubectl|gh|handoff-verify)\b"
)

_SUGGESTION_OR_HYPOTHETICAL_QUOTE_RE = re.compile(
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
    r"usage\s*:|run\s+this|after\s+applying"
    r")\b",
    re.IGNORECASE,
)

_COMMAND_INVOCATION_PREFIX_RE = re.compile(
    r"\b(?:ran|run|running|execute|executed|executing|invoked|invoke|via|using|command)\s*[:\-]?\s*$",
    re.IGNORECASE,
)

_OUTPUT_ATTRIBUTION_RE = re.compile(
    r"\b(?:"
    r"stdout|stderr|output(?:\s+shows|\s+is|\s+contains|\s+was|:)|"
    r"printed|prints|logged|logs|emitted|emits|returned|returns|"
    r"failed\s+with|error\s+message|traceback|reports|reported|"
    r"shows\s*:|produced|produces"
    r")\b",
    re.IGNORECASE,
)

_FENCE_FILE_ATTRIBUTION_RE = re.compile(
    r"\b(?:"
    r"from\s+(?:the\s+)?file|in\s+(?:the\s+)?file|currently\s+(?:reads|contains|has)|"
    r"current\s+(?:implementation|code)|exact\s+(?:code|lines?|content|snippet)|"
    r"verbatim|excerpt\s+from|lines?\s+\d+|"
    r"contains\s*:|reads\s*:|implementation\s+(?:in|from)"
    r")\b",
    re.IGNORECASE,
)

_FENCE_SKIP_LANGS: frozenset[str] = frozenset({"diff", "patch"})
_ELLIPSIS_PLACEHOLDER_RE = re.compile(r"(?:\.\.\.|<\w+>)")


def _is_multi_token_code_snippet(snippet: str) -> bool:
    """Return True if an inline backticked string is a verifiable multi-token code snippet."""
    s = snippet.strip()
    if len(s) < 8 or len(s) > 400:
        return False
    if _SINGLE_IDENT_RE.fullmatch(s) or _CLI_FLAG_RE.fullmatch(s):
        return False
    if _is_valid_file_path(s, has_line_spec=False):
        return False
    if "://" in s:
        return False
    # Skip abbreviated/placeholder snippets with `...` or `<placeholder>`
    if _ELLIPSIS_PLACEHOLDER_RE.search(s):
        return False
    # Must contain actual code structure OR multi-word error/output token with punctuation
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

    # Skip blocks containing ellipsis placeholder lines (`...`, `# ...`, `// ...`)
    for line in body.splitlines():
        ls = line.strip()
        if ls in ("...", "# ...", "// ...", "/* ... */", "<!-- ... -->"):
            return False, None, None, None

    # Inspect the last 2 non-empty lines immediately preceding the fence
    prev_lines = [ln.strip() for ln in block.preceding_text.splitlines() if ln.strip()][-2:]
    prev_context = " ".join(prev_lines)
    if not prev_context:
        return False, None, None, None

    # Skip hypothetical, suggested fix, example, or runnable shell-instruction blocks
    if _SUGGESTION_OR_HYPOTHETICAL_QUOTE_RE.search(prev_context):
        return False, None, None, None
    if block.lang in ("sh", "bash", "zsh", "shell", "console") and re.search(
        r"\b(?:run|execute|install|usage|command)\s*:?\s*$", prev_context, re.IGNORECASE
    ):
        return False, None, None, None

    # Check if preceding context cites a file pointer
    file_pointers = extract_pointer_claims(prev_context, base_dirs=base_dirs)
    if file_pointers:
        last_ptr = file_pointers[-1]
        return True, last_ptr.file_path, last_ptr.start_line, last_ptr.end_line

    # Check explicit output or file attribution cues
    if _OUTPUT_ATTRIBUTION_RE.search(prev_context) or _FENCE_FILE_ATTRIBUTION_RE.search(
        prev_context
    ):
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

    # 1. Inline backticked snippets tied to a cited file or command output in the same sentence
    for chunk in prose_chunks:
        for _, _, sentence in _iter_sentences(chunk.text):
            # Extract file pointers in this sentence first
            sentence_pointers = extract_pointer_claims(
                sentence, message_index=message_index, base_dirs=base_dirs
            )
            has_output_cue = bool(_OUTPUT_ATTRIBUTION_RE.search(sentence))
            if not sentence_pointers and not has_output_cue:
                continue

            # Collect pointer text spans so we never treat a file path itself as a quote
            pointer_texts = {p.text.strip("`") for p in sentence_pointers}
            pointer_paths = {p.file_path for p in sentence_pointers if p.file_path}

            for m_bt in _BACKTICK_PATH_RE.finditer(sentence):
                snippet = m_bt.group(1).strip()
                if not snippet:
                    continue
                if (
                    snippet in pointer_texts
                    or normalize_path(snippet, base_dirs=base_dirs) in pointer_paths
                ):
                    continue
                if _COLON_LINE_RE.fullmatch(snippet) or _HASH_LINE_RE.fullmatch(snippet):
                    continue

                # Skip shell command invocations like "ran `uv run pytest -v`"
                prefix = sentence[max(0, m_bt.start() - 80) : m_bt.start()]
                if _COMMAND_INVOCATION_PREFIX_RE.search(prefix) and _SHELL_CMD_START_RE.match(
                    snippet
                ):
                    continue

                # Skip suggestions / hypothetical / proposed replacement code in clause prefix
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

                # For output-attributed sentences, allow multi-word error/output strings
                is_code = _is_multi_token_code_snippet(snippet)
                is_output_str = (
                    has_output_cue
                    and len(snippet) >= 10
                    and not _SINGLE_IDENT_RE.fullmatch(snippet)
                    and not _CLI_FLAG_RE.fullmatch(snippet)
                    and not _SHELL_CMD_START_RE.match(snippet)
                    and not _ELLIPSIS_PLACEHOLDER_RE.search(snippet)
                    and (" " in snippet or ":" in snippet)
                )
                if not (is_code or is_output_str):
                    continue

                # Associate with the nearest preceding file pointer in the sentence (if any)
                assoc_file: str | None = None
                assoc_start: int | None = None
                assoc_end: int | None = None
                if sentence_pointers:
                    chosen = sentence_pointers[0]
                    best_pos = -1
                    for ptr in sentence_pointers:
                        pos = sentence.find(ptr.text)
                        if pos != -1 and pos < m_bt.start() and pos >= best_pos:
                            best_pos = pos
                            chosen = ptr
                    # When attributed to command output without a preceding file pointer,
                    # leave file_path=None so command stdout is checked.
                    if best_pos != -1 or not has_output_cue:
                        assoc_file = chosen.file_path
                        assoc_start = chosen.start_line
                        assoc_end = chosen.end_line

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

    # 2. Fenced code blocks explicitly claimed to come from a file or command output
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

_ABSENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bno\s+(?:other\s+|remaining\s+|further\s+|additional\s+|more\s+)?"
        r"(?:callers?|references?|usages?|uses|matches|occurrences?|instances?|hits|"
        r"consumers?|importers?|implementations?|definitions?|entries|results?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\s+(?:other|remaining|further|additional|more|open|matching)\s+"
        r"(?:issues?|items?|files?|prs?|pull\s+requests?|records?|pages?|tickets?|todos?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\s+(?:todos?|fixmes?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:zero|0)\s+(?:other\s+|remaining\s+)?"
        r"(?:callers?|references?|usages?|matches|occurrences?|instances?|hits|results?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:not|never)\s+(?:used|called|referenced|imported|invoked|found|present|defined)\s+"
        r"(?:anywhere(?:\s+else)?|elsewhere|in\s+(?:the\s+)?(?:codebase|repo|repository|project))\b",
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
        r"(?:used|called|referenced|imported)\s+(?:anywhere(?:\s+else)?|elsewhere)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bunused\s+(?:anywhere|across\s+(?:the\s+)?(?:codebase|repo|repository|project))\b",
        re.IGNORECASE,
    ),
)

_COMPLETENESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:checked|reviewed|inspected|audited|scanned|examined|searched|read|fetched|"
        r"listed|analyzed|processed|went\s+through|iterated\s+(?:through|over))\s+"
        r"(?:all(?:\s+of\s+the)?|every(?:\s+single)?)\s+(?:\d+\s+)?"
        r"(?:files?|pages?|results?|items?|matches|callers?|references?|usages?|issues?|"
        r"prs?|pull\s+requests?|records?|entries|occurrences?|modules?|packages?|"
        r"directories|folders?|endpoints?|handlers?|logs?|commits?|branches?|comments?)\b",
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
        r"\ball\s+\d+\s+(?:pages?|items?|results?|files?|issues?|prs?|pull\s+requests?|records?|entries|matches)\b"
        r"(?!\s+(?:pass|passed|passing|succeed|succeeded|fail|failed|failing))",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:all\s+(?:the\s+)?(?:pages|results|matching\s+files|occurrences|callers|references))\b"
        r"(?!\s+(?:pass|passed|passing|succeed|succeeded|fail|failed|failing))",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:checked|verified|reviewed)\s+all\s+(?:of\s+)?them\b",
        re.IGNORECASE,
    ),
)

_HEDGING_OR_NEGATION_RE = re.compile(
    r"\b(?:"
    r"cannot|can't|couldn't|could\s+not|unable\s+to|did\s+not|didn't|have\s+not|haven't|"
    r"not\s+sure|unclear|might|may\s+(?:still\s+)?(?:be|have)|whether|if\s+there\s+(?:are|were)|"
    r"without\s+(?:checking|searching|reviewing)|only\s+(?:checked|searched|reviewed)\s+(?:a\s+)?subset"
    r")\b",
    re.IGNORECASE,
)

# Explicit scope qualifiers (e.g. "in `src/auth`", "under src/auth/", "within `src/app.py`")
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


def extract_scope_qualifiers(
    sentence: str,
    *,
    base_dirs: Sequence[str] = (),
) -> list[str]:
    """Extract explicit directory/file scope qualifiers from a sentence (empty if repo-wide)."""
    qualifiers: list[str] = []
    seen: set[str] = set()

    def _add(raw: str, *, allow_any_dir_name: bool = False) -> None:
        if not _is_plausible_scope_path(raw, allow_any_dir_name=allow_any_dir_name):
            return
        norm = normalize_path(raw.rstrip("/"), base_dirs=base_dirs)
        if norm and norm not in (".", "./") and norm not in seen:
            seen.add(norm)
            qualifiers.append(norm)

    for m in _SCOPE_QUALIFIER_BACKTICK_RE.finditer(sentence):
        raw = m.group(1).strip()
        # Strip optional `:line` suffix if someone wrote "in `src/app.py:1-50`"
        raw_clean = re.sub(r"(?::\d+(?:-\d+)?|#L\d+(?:-L?\d+)?)$", "", raw)
        _add(raw_clean)

    for m in _SCOPE_QUALIFIER_BARE_PATH_RE.finditer(sentence):
        raw = m.group("path").strip().rstrip(".,;:")
        if raw.lower() in _REPO_WIDE_NOUNS:
            continue
        _add(raw)

    for m in _SCOPE_QUALIFIER_NAMED_DIR_RE.finditer(sentence):
        raw = m.group("dir").strip()
        if raw.lower() in _REPO_WIDE_NOUNS:
            continue
        _add(raw, allow_any_dir_name=True)

    # Also handle "in this file" when a file pointer is explicitly present in the same sentence
    if not qualifiers and re.search(r"\b(?:in|within)\s+this\s+file\b", sentence, re.IGNORECASE):
        ptrs = extract_pointer_claims(sentence, base_dirs=base_dirs)
        if ptrs and ptrs[0].file_path:
            _add(ptrs[0].file_path)

    return qualifiers


def _has_hedging_before(sentence: str, match_start: int) -> bool:
    prefix = sentence[max(0, match_start - 70) : match_start]
    # Trim at clause boundary
    for sep in (";", " — ", ". "):
        idx = prefix.rfind(sep)
        if idx != -1:
            prefix = prefix[idx + len(sep) :]
    return bool(_HEDGING_OR_NEGATION_RE.search(prefix))


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

    for chunk in prose_chunks:
        for _, _, sentence in _iter_sentences(chunk.text):
            qualifiers = extract_scope_qualifiers(sentence, base_dirs=base_dirs)
            primary_scope = qualifiers[0] if qualifiers else None

            # Absence claims
            for pat in _ABSENCE_PATTERNS:
                m = pat.search(sentence)
                if not m:
                    continue
                if _has_hedging_before(sentence, m.start()):
                    continue
                phrase = m.group(0).strip()
                # Skip "no files were modified/changed/edited" (not a search absence claim)
                after = sentence[m.end() : m.end() + 30]
                if re.match(
                    r"^\s+(?:were|was|have\s+been|had\s+been)\s+(?:modified|changed|edited|touched|deleted|added|created|updated)\b",
                    after,
                    re.IGNORECASE,
                ):
                    continue
                key = ("absence", sentence)
                if key not in seen:
                    seen.add(key)
                    claims.append(
                        Claim(
                            kind="absence",
                            text=phrase,
                            file_path=primary_scope,
                            context=sentence,
                            message_index=message_index,
                        )
                    )
                break

            # Completeness claims
            for pat in _COMPLETENESS_PATTERNS:
                m = pat.search(sentence)
                if not m:
                    continue
                if _has_hedging_before(sentence, m.start()):
                    continue
                phrase = m.group(0).strip()
                key = ("completeness", sentence)
                if key not in seen:
                    seen.add(key)
                    claims.append(
                        Claim(
                            kind="completeness",
                            text=phrase,
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

# handoff-verify

**Deterministic, zero-LLM claim & scope verification for AI coding agents and subagents.**

AI coding agents and subagents frequently hand back confident reports containing subtle ungrounded claims: citing `path:line` pointers they never read (or read before a line-shifting edit), altering quoted code snippets, or claiming *"no other callers anywhere in the codebase"* after a truncated or subdirectory-scoped grep — or *"reviewed all pages"* while leaving a pagination cursor unread.

`handoff-verify` parses an agent's tool-call transcript and deterministically checks every claim in its final handoff against what the agent actually observed:

- **File pointers (`path:line`, `path:start-end`, `#L10-L20`):** Was the file actually read? Were the cited lines inside the read window (`offset`/`limit`)? Was the file edited afterward without re-reading (or changed on disk when `--repo` is supplied)?
- **Verbatim quotes (inline & fenced):** Does the quoted snippet actually appear in what the agent read, wrote, or observed in command output (exact or whitespace/operator-normalized)?
- **Absence & completeness scope receipts:** When an agent claims *"no other callers"*, *"no matches"*, *"not used anywhere"*, or *"reviewed all pages"*, did it actually run an untruncated search across the claimed scope, and were all pagination cursors followed?

**Zero LLM calls. Zero third-party runtime dependencies (Python 3.11+ stdlib only). Sub-millisecond verification. Quiet when clean, loud on anomalies.**

---

## Installation

Requires Python `>=3.11`.

```bash
# Run directly without installing
uvx handoff-verify --version

# Install globally as a CLI tool with uv
uv tool install handoff-verify

# Or install with pip
pip install handoff-verify
```

### From source

```bash
git clone https://github.com/1solomonwakhungu/handoff-verify.git
cd handoff-verify
uv sync --dev
uv run handoff-verify --version
```

---

## 60-Second Quickstart

Inspect what a transcript actually read, edited, searched, truncated, or left unread:

```bash
handoff-verify inspect session.jsonl
handoff-verify inspect session.jsonl --format json
```

Verify claims in a transcript (pass `-` to read from stdin):

```bash
# Compact plain-text receipt (exit 0 on pass, exit 1 on findings, exit 2 on usage/parse error)
handoff-verify check session.jsonl

# Stay completely silent when clean
handoff-verify check session.jsonl --quiet-on-pass

# Also cross-check cited files and line counts against the live repository on disk
handoff-verify check session.jsonl --repo /path/to/repo

# Treat warnings (e.g. non-trivial excluded search dirs) as failures
handoff-verify check session.jsonl --strict

# Render actionable Markdown (great for parent agents / PR comments) or machine-readable JSON
handoff-verify check session.jsonl --format markdown
handoff-verify check session.jsonl --format json
```

Run the built-in 24-case seeded evaluation suite:

```bash
handoff-verify eval
handoff-verify eval --verbose
handoff-verify eval --format markdown
handoff-verify eval --format json
```

---

## Claude Code Hook Integration

Wire `handoff-verify` into Claude Code's `SubagentStop` and `Stop` lifecycle hooks so ungrounded subagent or main-agent handoffs are caught automatically.

### Print or install `.claude/settings.json`

```bash
# Preview the hook configuration JSON
handoff-verify init --claude-code

# Safely create or merge into .claude/settings.json (preserves existing hooks and keys)
handoff-verify init --claude-code --write

# Use block mode (exit 2 on findings) or a custom wrapper command (e.g. uvx)
handoff-verify init --claude-code --write --mode block
handoff-verify init --claude-code --write --command "uvx handoff-verify"
```

Generated configuration (`handoff-verify init --claude-code`):

```json
{
  "hooks": {
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "handoff-verify hook claude-code --mode advise"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "handoff-verify hook claude-code --mode advise"
          }
        ]
      }
    ]
  }
}
```

### Hook modes & safety guarantees

- **`SubagentStop` vs `Stop` isolation:** On `SubagentStop`, `handoff-verify` strictly reads `agent_transcript_path` (never falling back to the parent session's `transcript_path`). On `Stop`, it reads `transcript_path`.
- **Quiet when clean:** When verification passes with zero findings, the hook exits `0` with empty stdout and stderr.
- **`--mode advise` (default):** On failing claims, exits `0` and emits structured Claude Code JSON (`systemMessage` + `hookSpecificOutput.additionalContext`) with concise, bounded Markdown feedback.
- **`--mode block`:** On failing claims, writes the concise actionable receipt to stderr and exits `2` so the agent fixes the ungrounded claim before stopping.
- **Infinite-loop guard (`stop_hook_active`):** When Claude Code sets `stop_hook_active: true`, the hook **never** exits `2` and **never** emits `hookSpecificOutput.additionalContext` — it emits `systemMessage` only and exits `0`, guaranteeing the agent can always stop after at most one verification retry.
- **Fail-open by default:** Parse, I/O, missing-file, or internal errors log a single diagnostic line to stderr and exit `0` so a hook failure never bricks a coding session. Pass `--fail-closed` to exit `2` on errors instead.

---

## Real CLI Output Examples

### Clean session (`exit 0`)

```console
$ handoff-verify check evals/cases/clean_01_claude_single_read.jsonl
PASS: 0 findings (6 claims checked, 1 file read, 1.0ms)
```

### Truncated search claimed as *"no other matches"* (`exit 1`, text format)

```console
$ handoff-verify check evals/cases/fail_06_claude_truncated_grep.jsonl
FAIL: 1 finding (1 error; 4 claims checked, 0 files read, 0.7ms)
  [ERROR] TRUNCATED_SEARCH_OUTPUT: Claimed `no other matches`, but search output was truncated: Grep#0 (result limit (5) reached).
    Claim (absence): no other matches
    Evidence: truncated_searches=[{event_index=0, tool_name=Grep, query=deprecated_v1_client, reason=result limit (5) reached}]
    Next step: Re-run the search without head_limit/truncation before claiming absence or completeness.
Scope receipt:
  Searched roots: .
  Truncated outputs (1): [0] Grep (result limit (5) reached)
```

### Subagent with unread pointer + fabricated quote (`exit 1`, Markdown format)

```console
$ handoff-verify check evals/cases/fail_11_claude_subagent_fabricated.jsonl --format markdown
```

```markdown
### handoff-verify: FAIL

**Summary:** 2 findings (2 errors) · 3 claims checked · 1 file read · 0.6ms

#### Findings

- **[ERROR] `UNREAD_FILE`** — Cited `src/security/cors.py` (line 34), but the file was never read in transcript.
  - **Claim (`pointer`):** `` `src/security/cors.py:34` ``
  - **Evidence:** file_path=src/security/cors.py; claimed_range=[34, 34]; observed_in_search=false; reason=never read in transcript
  - **Next step:** Read line 34 of src/security/cors.py
- **[ERROR] `QUOTE_NOT_FOUND`** — Quoted snippet `if origin not in ALLOWED_WILDCARD_ORIGINS:` was not found in observed content of `src/security/csrf.py`.
  - **Claim (`quote`):** `` `if origin not in ALLOWED_WILDCARD_ORIGINS:` `` (`src/security/csrf.py:6-9`)
  - **Evidence:** claimed_file=src/security/csrf.py; checked_events=[file_read#0]; snippet_preview=if origin not in ALLOWED_WILDCARD_ORIGINS:
  - **Next step:** Re-read `src/security/csrf.py` and verify the exact code before quoting it.
```

---

## JSON Output Schema (`schema_version: "1.0"`)

`handoff-verify check <transcript> --format json` emits a stable machine-readable report:

```json
{
  "schema_version": "1.0",
  "passed": false,
  "transcript_path": "evals/cases/fail_01_claude_unread_pointer.jsonl",
  "source_format": "claude_code",
  "duration_ms": 0.65,
  "summary": {
    "errors": 1,
    "warnings": 0,
    "infos": 0,
    "claims": 2,
    "findings": 1
  },
  "claims": [
    {
      "kind": "pointer",
      "text": "`src/auth/session_store.py:48-55`",
      "file_path": "src/auth/session_store.py",
      "start_line": 48,
      "end_line": 55,
      "snippet": null,
      "context": "Revoked session IDs are then checked against Redis in `src/auth/session_store.py:48-55` before granting access.",
      "message_index": 0
    }
  ],
  "findings": [
    {
      "severity": "error",
      "code": "UNREAD_FILE",
      "claim": { "...": "..." },
      "evidence": {
        "file_path": "src/auth/session_store.py",
        "claimed_range": [48, 55],
        "observed_in_search": false,
        "reason": "never read in transcript"
      },
      "message": "Cited `src/auth/session_store.py` (lines 48-55), but the file was never read in transcript.",
      "suggestion": "Read lines 48-55 of src/auth/session_store.py"
    }
  ],
  "scope_receipt": {
    "searched_roots": [],
    "excluded_dirs": [],
    "truncated_outputs": [],
    "unread_cursors": [],
    "files_read": ["/workspace/payments/src/auth/tokens.py"],
    "files_edited": []
  }
}
```

### Finding codes

| Code | Severity | Meaning |
| --- | --- | --- |
| `UNREAD_FILE` | `error` | Cited file or `path:line` was never read/observed in the transcript |
| `LINE_OUT_OF_READ_WINDOW` | `error` | Cited line range falls outside the transcript's observed `Read` line windows |
| `STALE_FILE_READ` | `error` / `warning` | File was edited or deleted after the last read (or changed on disk with `--repo`) without re-reading |
| `FILE_NOT_FOUND` | `error` | Cited file does not exist on disk in `--repo` |
| `LINE_OUT_OF_BOUNDS` | `error` | Cited line number exceeds the live file's line count in `--repo` |
| `QUOTE_NOT_FOUND` | `error` | Quoted code or command output snippet does not appear in observed evidence |
| `QUOTE_MISMATCH` | `error` | Quoted snippet was attributed to file A, but was only observed in file B |
| `NO_SEARCH_EVIDENCE` | `error` | Absence/completeness claim made with no matching repository search |
| `TRUNCATED_SEARCH_OUTPUT` | `error` | Absence/completeness claim backed only by truncated search or partial read output |
| `NARROW_SEARCH_SCOPE` | `error` | Repo-wide (or wider) absence claim backed only by a subdirectory search |
| `UNREAD_PAGINATION_CURSOR` | `error` | Completeness/absence claim made while a tool returned `has_more` / `nextCursor` that was never followed |
| `EXCLUDED_SEARCH_DIRS` | `warning` | Search explicitly excluded non-trivial directories not acknowledged in the claim |

---

## 24-Case Seeded Eval Results

Measured via `uv run handoff-verify eval --format markdown` on the bundled 24-case eval suite (`evals/manifest.json` + `evals/cases/`, 12 clean sessions and 12 planted failure modes across Claude Code, Codex CLI, and Generic JSONL):

| Metric | Measured | Target |
| --- | ---: | ---: |
| Cases | 24 (12 clean / 12 failures) | 24 |
| Precision | **100.0%** (12 TP, 0 FP) | 100.0% |
| Recall | **100.0%** (12/12) | >= 90.0% |
| False Positive Rate | **0.0%** (0/12) | 0.0% |
| Expected Code Match | **100.0%** | 100.0% |
| Median Latency | **0.40 ms** | < 50.0 ms |
| P95 / Max Latency | **0.82 ms / 1.15 ms** | — |

| Category | Total | Clean | Failures | TP | TN | FP | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `cross_harness` | 4 | 2 | 2 | 2 | 2 | 0 | 0 |
| `false_positive_guard` | 1 | 1 | 0 | 0 | 1 | 0 | 0 |
| `pagination` | 2 | 1 | 1 | 1 | 1 | 0 | 0 |
| `pointers` | 6 | 3 | 3 | 3 | 3 | 0 | 0 |
| `quotes` | 4 | 2 | 2 | 2 | 2 | 0 | 0 |
| `scope` | 5 | 2 | 3 | 3 | 2 | 0 | 0 |
| `subagent` | 2 | 1 | 1 | 1 | 1 | 0 | 0 |

---

## Supported Transcript Formats

`handoff-verify` auto-detects the transcript format by default (override with `--parser claude_code|codex|generic`):

1. **Claude Code JSONL (`claude_code`)** — Main session and sidechain subagent logs (`assistant` / `user` records with `tool_use` / `tool_result` blocks for `Read`, `Grep`, `Glob`, `LS`, `Bash`, `Edit`, `MultiEdit`, `Write`, `NotebookEdit`, `WebFetch`, and `mcp__*` paginated tools).
2. **Codex CLI JSONL (`codex`)** — Modern rollout logs (`session_meta`, `turn_context`, `response_item` with `function_call` / `function_call_output`, `custom_tool_call` / `apply_patch`, `local_shell_call`, `event_msg`) as well as legacy flat logs.
3. **Generic JSONL (`generic`)** — A simple, documented line-delimited JSON schema for integrating any custom agent harness:

```jsonl
{"type": "meta", "cwd": "/workspace/app"}
{"type": "file_read", "file_path": "src/auth.py", "start_line": 1, "end_line": 20, "content": "def verify(tok):\n    return bool(tok)"}
{"type": "search", "tool_name": "rg", "query": "verify", "search_path": ".", "match_count": 1, "matched_files": ["src/auth.py"], "output": "src/auth.py:1:def verify(tok):", "truncated": false}
{"type": "command", "command": "pytest", "stdout": "4 passed in 0.12s\n", "exit_code": 0}
{"type": "assistant", "text": "Verified `src/auth.py:1-2` (`return bool(tok)`). No other callers of `verify` in the repository."}
```

Supported generic record types: `meta`, `file_read`, `file_edit`, `search`, `command`, `pagination`, `tool_call` / `tool_result`, and `assistant`.

---

## Development

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
uv run handoff-verify eval --verbose
```

## License

MIT — see [LICENSE](LICENSE).

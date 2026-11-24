# handoff-verify

**Deterministic, zero-LLM claim & scope verification for AI coding agents and subagents.**

AI coding agents and subagents frequently hand back confident reports containing subtle ungrounded claims: citing `path:line` pointers they never read (or read before a line-shifting edit), altering quoted code snippets, or claiming *"no other callers anywhere in the codebase"* after a truncated or subdirectory-scoped grep — or *"reviewed all pages"* while leaving a pagination cursor unread.

`handoff-verify` parses an agent's tool-call transcript and deterministically checks every claim in its final handoff against what the agent actually observed:

- **File pointers (`path:line`, `path:start-end`, `#L10-L20`):** Was the file actually read? Were the cited lines inside the read window (`offset`/`limit`)? Was the file edited afterward without re-reading (or changed on disk when `--repo` is supplied)?
- **Verbatim quotes (inline & fenced):** Does the quoted snippet actually appear in what the agent read, wrote, or observed in command output (exact or whitespace/operator-normalized)?
- **Absence & completeness scope receipts:** When an agent claims *"no other callers"*, *"no matches"*, *"not used anywhere"*, or *"reviewed all pages"*, did it actually run an untruncated search across the claimed scope, and were all pagination cursors followed?

**Zero LLM calls. Zero third-party runtime dependencies (Python 3.11+ stdlib only). Fast linear-time verification (sub-millisecond on small transcripts; tens of milliseconds on multi-megabyte transcripts). Silent on clean passes (`--quiet-on-pass` or hook mode) with actionable findings on ungrounded claims.**

---

## Installation

Requires Python `>=3.11`. No runtime dependencies.

### From PyPI (recommended)

```bash
uvx handoff-verify --version           # run with zero install
uv tool install handoff-verify         # install globally on PATH
pip install handoff-verify
```

### As an Agent Skill ([skills.sh](https://skills.sh))

Install into Claude Code, Codex, Cursor, Antigravity, OpenCode, and other skill-compatible agents:

```bash
npx skills add 1solomonwakhungu/handoff-verify
```

### As a Claude Code plugin (bundled hooks + skill)

```bash
claude plugin marketplace add 1solomonwakhungu/handoff-verify
claude plugin install handoff-verify@handoff-verify
```

Or test locally from a clone without installing:

```bash
claude --plugin-dir /path/to/handoff-verify
```

The plugin bundles `Stop` and `SubagentStop` hooks (`hooks/hooks.json`), a standalone `bin/handoff-verify` launcher (uses system Python 3.11+ or `uv`-managed Python against the bundled `src/`), and `skills/handoff-verify/SKILL.md`.

### From source

```bash
git clone https://github.com/1solomonwakhungu/handoff-verify.git
cd handoff-verify
uv tool install .            # puts `handoff-verify` on your PATH
handoff-verify --version
handoff-verify doctor        # verify PATH, Python >= 3.11, and Claude hook config
```

---

## 60-Second Quickstart

Check the Claude Code session you just ran, from the project directory:

```bash
handoff-verify check --latest
```

`--latest` picks the newest transcript for the current directory. To check a specific one,
pass its path. Transcripts live here:

| Harness | Transcript location |
| --- | --- |
| Claude Code | `~/.claude/projects/<cwd-with-dashes>/<session-id>.jsonl` |
| Claude Code subagent | `~/.claude/projects/<cwd-with-dashes>/<session-id>/subagents/agent-<id>.jsonl` |
| Codex CLI | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |

Inspect what a transcript actually read, edited, searched, truncated, or left unread:

```bash
handoff-verify inspect session.jsonl
handoff-verify inspect session.jsonl --json
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
handoff-verify check session.jsonl --json          # same as --format json
```

Run `handoff-verify <subcommand> --help` for flags, examples, and exit codes.

Run the bundled 32-case evaluation suite (24 seeded cases + 8 realistic messy-session cases, shipped in both source checkouts and built wheels; pass `--cases-dir <path>` to run a custom local suite):

```bash
# Exit 0 when all targets pass, exit 1 if targets are missed, exit 2 on missing/invalid manifest
handoff-verify eval
handoff-verify eval --verbose
handoff-verify eval --strict
handoff-verify eval --format markdown
handoff-verify eval --json          # same as --format json
handoff-verify eval --cases-dir ./custom-evals
```

Verify your local setup (PATH, Python version, and `.claude/settings.json` hook config):

```bash
handoff-verify doctor
handoff-verify doctor --json
```

---

## Claude Code Hook Integration

Wire `handoff-verify` into Claude Code's `SubagentStop` and `Stop` lifecycle hooks so ungrounded subagent or main-agent handoffs are caught automatically.

### Print or install `.claude/settings.json`

```bash
# Preview the hook configuration JSON
handoff-verify init

# Safely create or merge into .claude/settings.json (preserves existing hooks and keys)
handoff-verify init --write

# Use block mode (exit 2 on findings) or a custom wrapper command (e.g. uvx)
handoff-verify init --write --mode block
handoff-verify init --write --command "uvx handoff-verify"

# Install for every project instead of this one
handoff-verify init --write --settings ~/.claude/settings.json
```

`init --write` warns if the hook command is not on your PATH, because Claude Code would then
report "command not found" on every stop. Start with the default `advise` mode; see
[How it behaves on real sessions](#how-it-behaves-on-real-sessions) before choosing `block`.

Generated configuration (`handoff-verify init`):

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
- **Verifies the message Claude Code is about to hand back:** Claude Code can fire `Stop` / `SubagentStop` before the final assistant message is written to the transcript file. The hook compares the transcript with the payload's `last_assistant_message`, waits up to one second for the file to catch up, and otherwise verifies the payload text against the tool evidence already on disk.
- **Quiet when clean and on warning-only reports:** When verification passes with zero findings — or with warning-only findings (unless `--include-warnings` or `--strict` is passed) — the hook exits `0` with empty stdout and stderr.
- **`--mode advise` (default):** On error-level findings (or warnings with `--strict`), exits `0` and emits structured Claude Code JSON (`systemMessage` + `hookSpecificOutput.additionalContext`, requires a Claude Code version supporting `additionalContext` on `Stop`) with concise, bounded Markdown feedback. When `--repo` is omitted, the hook defaults `--repo` to the payload `cwd`.
- **`--mode block`:** On failing claims, writes the concise actionable receipt to stderr and exits `2` so the agent fixes the ungrounded claim before stopping.
- **Infinite-loop guard (`stop_hook_active`):** When Claude Code sets `stop_hook_active: true`, the hook **never** exits `2` and **never** emits `hookSpecificOutput.additionalContext` — it emits `systemMessage` only and exits `0`, guaranteeing the agent can always stop after at most one verification retry.
- **Fail-open with visible diagnostics by default:** Parse, I/O, missing-file, usage, or internal errors log a single diagnostic line to stderr and exit `1` (non-blocking in Claude Code while surfacing stderr, instead of silently hiding errors behind exit `0`). Pass `--fail-closed` to exit `2` (blocking) on errors instead.

---

## Using it from an agent

[`skills/handoff-verify/SKILL.md`](https://github.com/1solomonwakhungu/handoff-verify/blob/main/skills/handoff-verify/SKILL.md) is a ready-made skill that
tells an agent when to run the check, how to read a finding, and what to do about each code.
Copy the folder into `.claude/skills/` (project) or `~/.claude/skills/` (every project):

```bash
mkdir -p .claude/skills && cp -R skills/handoff-verify .claude/skills/
```

An agent can audit a finished subagent transcript (or a past session) from the shell:

```bash
handoff-verify check <subagent-transcript.jsonl> --repo . --format markdown --quiet-on-pass
```

Exit `0` means no error-level findings (warnings may still print unless `--strict` is passed; `--quiet-on-pass` prints nothing only at 0 findings). On exit `1`, each finding includes its `severity`, `code`, `claim`, and a `suggestion` naming the read or search that would ground it. Because `check` reads the last assistant text already flushed to the JSONL file, use the `Stop` hook (not `check --latest`) to verify an agent's own unsent final turn.

---

## How it behaves on real sessions

The bundled eval suite below is a deterministic regression benchmark. As a calibration check, the verifier was also run over 1,571 real Claude Code transcripts from one developer's machine (972 main sessions, 599 subagents; mostly code-review and implementation work):

| Stage | Transcripts with an error-level finding |
| --- | ---: |
| As first published to PyPI (`0.1.0`) | 512 / 1,571 (32.6%) |
| After shell-read & scratch-copy fixes | 343 / 1,571 (21.8%) |

Follow-up fixes addressed the top remaining false-alarm classes observed in that corpus:

- shell output redirected to a temp/log file (`> /tmp/x.log`, `| tee ...`) and subsequently read back
- `grep -n` / `rg -n` context lines (`-C`/`-A`/`-B` `file-line-content`) and lines immediately adjacent to matched hits
- parent agents relaying `Agent`/`Task` subagent `path:line` ranges under alternate path spellings or unambiguous basenames
- negated existence mentions (*"`src/app.py:99` does not exist"*, *"there is no `foo.py:42`"*, *"`bar.py` wasn't found"*)
- non-code absence phrases (*"no usage limit"*, *"no open issue"*, *"no open PRs"*, *"no errors"*, *"no warnings"*, *"no changes"*)

Remaining caveats to keep in mind:

- Reads through arbitrary scripts (`python - <<EOF` generating or slicing files) cannot always be mapped back to exact line numbers, and files generated by a script that are linked in prose may still be flagged until read directly.
- `--latest` currently discovers Claude Code session directories; for Codex CLI rollouts pass the transcript path explicitly.
- Treat any finding as *"the transcript does not show this"*, not *"this is false"*. Start with the default `advise` mode before opting into `block` mode.

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
      "text": "`src/auth/tokens.py:3-6`",
      "file_path": "src/auth/tokens.py",
      "start_line": 3,
      "end_line": 6,
      "snippet": null,
      "context": "Bearer headers are parsed in `src/auth/tokens.py:3-6`.",
      "message_index": 0
    },
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

## Benchmarks & 32-Case Seeded Eval Results

### Seeded evaluation suite (`handoff-verify eval`)

Measured via `uv run handoff-verify eval --format markdown` on macOS (Apple Silicon / `arm64`, Python 3.13.14) across the bundled 32-case **seeded evaluation suite** (`evals/manifest.json` + `evals/cases/`: 16 clean sessions and 16 planted failures across Claude Code, Codex CLI, and Generic JSONL, including 8 real-session-shaped `real_session` regression fixtures covering redirected log readbacks, `rg -n -C` / adjacent grep lines, parent/subagent path relays, negated mentions, and non-code absence phrases). Because these cases were authored alongside the verifier as a deterministic regression benchmark rather than a held-out production corpus, treat the table below as baseline regression coverage rather than a guarantee on arbitrary real-world phrasings.

| Metric | Measured (macOS arm64 / Python 3.13.14) | Target |
| --- | ---: | ---: |
| Cases | 32 (16 clean / 16 failures) | — |
| Precision | **100.0%** (16 TP, 0 FP) | 100.0% |
| Recall | **100.0%** (16/16) | >= 90.0% |
| False Positive Rate | **0.0%** (0/16) | 0.0% |
| Expected Code Match | **100.0%** | 100.0% |
| Median Latency | **0.60 ms** | < 50.0 ms |
| P95 / Max Latency | **1.38 ms / 1.56 ms** | — |

| Category | Total | Clean | Failures | TP | TN | FP | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `cross_harness` | 4 | 2 | 2 | 2 | 2 | 0 | 0 |
| `false_positive_guard` | 1 | 1 | 0 | 0 | 1 | 0 | 0 |
| `pagination` | 2 | 1 | 1 | 1 | 1 | 0 | 0 |
| `pointers` | 6 | 3 | 3 | 3 | 3 | 0 | 0 |
| `quotes` | 4 | 2 | 2 | 2 | 2 | 0 | 0 |
| `real_session` | 8 | 4 | 4 | 4 | 4 | 0 | 0 |
| `scope` | 5 | 2 | 3 | 3 | 2 | 0 | 0 |
| `subagent` | 2 | 1 | 1 | 1 | 1 | 0 | 0 |

### Scaling & CLI latency

Measured on the same machine (macOS `arm64`, Python 3.13.14):

| Workload | Transcript size | Parse | Verify | Total (median) | Throughput |
| --- | ---: | ---: | ---: | ---: | ---: |
| Small session (25 reads × 100 lines) | 115 KB | 1.28 ms | 1.22 ms | **2.50 ms** | 44.8 MB/s |
| Medium session (250 reads × 100 lines) | 1.14 MB | 9.98 ms | 2.73 ms | **12.74 ms** | 89.4 MB/s |
| Large session (1,250 reads × 100 lines) | 5.76 MB | 48.53 ms | 10.03 ms | **60.77 ms** | 94.7 MB/s |
| End-to-end CLI (`handoff-verify check clean_01 --quiet-on-pass`, 15 runs) | 1.6 KB | — | — | **56.8 ms** | *(~54.7 ms Python startup)* |

Reproduce the eval and scaling numbers locally:

```bash
uv run handoff-verify eval --format markdown
uv run python -c '
import json, statistics, tempfile, time
from pathlib import Path
from handoff_verify.parsers import auto_parse
from handoff_verify.pipeline import verify_transcript

with tempfile.TemporaryDirectory() as td:
    for n in (25, 250, 1250):
        p = Path(td) / f"b{n}.jsonl"
        recs = []
        for i in range(n):
            body = "\n".join(f"{ln:6d}→def fn_{i}_{ln}(x): return x + {ln}" for ln in range(1, 101))
            recs += [
                {"type": "assistant", "cwd": "/repo", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": f"/repo/src/p{i%20}/m{i}.py"}}]}},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": body}]}},
            ]
        recs += [
            {"type": "assistant", "cwd": "/repo", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "g0", "name": "Grep", "input": {"pattern": "legacy_fn", "output_mode": "content"}}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "g0", "content": "No matches found"}]}},
        ]
        cited = ", ".join(f"`src/p{i%20}/m{i}.py:1-10`" for i in range(10))
        recs.append({"type": "assistant", "cwd": "/repo", "message": {"role": "assistant", "content": [{"type": "text", "text": f"Verified {cited} (`def fn_9_1(x): return x + 1`). No callers of `legacy_fn` anywhere in the codebase."}]}})
        raw = "\n".join(json.dumps(r) for r in recs) + "\n"
        p.write_text(raw, encoding="utf-8")
        times = []
        for _ in range(7):
            t0 = time.perf_counter()
            rep = verify_transcript(auto_parse(p))
            times.append((time.perf_counter() - t0) * 1000)
            assert rep.passed and not rep.findings
        med = statistics.median(times)
        mb = len(raw.encode()) / (1024 * 1024)
        print(f"n={n:4d} ({mb:.2f} MB): median={med:.2f}ms ({mb / (med / 1000):.1f} MB/s)")
'
```

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

MIT — see [LICENSE](https://github.com/1solomonwakhungu/handoff-verify/blob/main/LICENSE).

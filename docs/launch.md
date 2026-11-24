# Launch Copy & Release Notes — handoff-verify

All numbers below come directly from the bundled 32-case eval suite (`uv run handoff-verify eval --format markdown`), local scaling measurements on macOS `arm64` / Python 3.13.14, and the 1,571-transcript calibration check documented in `README.md`.

---

## Show HN

**Title:**
Show HN: handoff-verify – Zero-LLM verifier for AI agent handoff claims

**Body:**
Hi HN,

I built `handoff-verify` because coding agents and subagents kept handing me confident summaries with small grounding bugs: citing `path:line` numbers they never read (or read before a line-shifting edit), misquoting a constant from memory, claiming "no other callers anywhere in the codebase" after a `head_limit=5` or subdirectory-only grep, or saying "reviewed all pages" while leaving an MCP pagination cursor unread.

Using another LLM as a judge for this felt slow, expensive, and non-deterministic, because all the ground truth is already sitting on disk in the session JSONL transcript.

`handoff-verify` is a zero-dependency Python 3.11+ CLI and Claude Code hook that parses Claude Code, Codex CLI, and generic JSONL transcripts and checks three classes of claims deterministically:

1. File pointers (`path:line`, `path:start-end`, `#L10-L20`): checks whether the file was read, whether the cited lines fell inside the `offset`/`limit` window, whether a later edit shifted those lines without a re-read, and (with `--repo`) whether the file exists and matches disk.
2. Verbatim quotes (inline and fenced): checks that quoted snippets appear in what the agent actually read, wrote, or saw in command output, with whitespace/operator normalization and file-attribution checking (`QUOTE_MISMATCH`).
3. Absence and completeness claims ("no other callers", "not used anywhere", "reviewed all pages"): builds a scope receipt from actual searches and pagination calls, flagging missing searches, truncated outputs, subdirectory-only searches, and unfollowed `nextCursor` / `has_more` responses.

Example on a truncated grep claimed as "no other matches" (timing illustrative):

```console
$ handoff-verify check evals/cases/fail_06_claude_truncated_grep.jsonl
FAIL: 1 finding (1 error; 4 claims checked, 0 files read, 0.9ms)
  [ERROR] TRUNCATED_SEARCH_OUTPUT: Claimed `no other matches`, but search output was truncated: Grep#0 (result limit (5) reached).
    Claim (absence): no other matches
    Evidence: truncated_searches=[{event_index=0, tool_name=Grep, query=deprecated_v1_client, reason=result limit (5) reached}]
    Next step: Re-run the search without head_limit/truncation before claiming absence or completeness.
Scope receipt:
  Searched roots: .
  Truncated outputs (1): [0] Grep (result limit (5) reached)
```

Try it on your last Claude Code session without installing anything:

```bash
uvx handoff-verify check --latest
```

Or wire it into Claude Code `Stop` and `SubagentStop` hooks (defaults to `advise` mode, silent on clean passes, loop-guarded via `stop_hook_active`):

```bash
uv tool install handoff-verify
handoff-verify init --write
```

Measured numbers (macOS arm64, Python 3.13.14):
- Bundled 32-case seeded regression suite (16 clean, 16 planted failures across Claude Code, Codex CLI, and generic JSONL): 100% precision (16 TP, 0 FP), 100% recall (16/16), 0.60 ms median latency.
- Scaling on synthetic transcripts: 115 KB in 2.5 ms, 1.14 MB in 12.7 ms, 5.76 MB in 60.8 ms (~95 MB/s). End-to-end CLI wall time is ~57 ms (of which ~55 ms is Python startup).
- Calibration on 1,571 real Claude Code sessions from my own machine cut error-level flags from 32.6% (512/1,571) down to 21.8% (343/1,571) after fixing shell-read and scratch-copy edge cases, followed by fixes for redirected log readbacks, `rg -C` context lines, parent/subagent path relays, negated existence mentions, and non-code absence phrases.

Limitations up front: it checks grounding against the transcript, not whether the code is right or tests pass. Inline Python one-liners that slice files cannot always map back to exact line numbers. Treat a finding as "the transcript does not show this" and start with `advise` mode before turning on `block`.

Repo: https://github.com/1solomonwakhungu/handoff-verify
PyPI: `pip install handoff-verify` / `uvx handoff-verify`

Happy to hear what real-session false positives or harness formats you hit.

---

## Reddit

### Post 1: `r/ClaudeAI`

**Title:**
I built a zero-LLM Claude Code Stop/SubagentStop hook that catches hallucinated `path:line` citations, misquoted code, and truncated-grep "no other callers" claims

**Body:**
Subagents are great for exploring a repo until one hands back a clean bullet list citing `src/auth/session_store.py:48-55` that it never opened, or says "no other callers anywhere in the codebase" when its Grep call had `head_limit=5` or only searched `src/auth/`.

Because Claude Code already writes every tool call and tool result to a local JSONL transcript, I wrote `handoff-verify` to check the final message against the transcript deterministically with Python's standard library (no LLM calls, no third-party dependencies).

What it checks:
- `path:line` and `#L10-L20` citations against actual `Read` windows (`offset`/`limit`), `grep -n` / `git diff` outputs, and post-edit line shifts.
- Inline and fenced code quotes against what the agent actually read, wrote, or saw in Bash output.
- Absence and completeness claims ("no other callers", "not used anywhere", "reviewed all pages") against actual search roots, truncation flags, and unread MCP pagination cursors.

How to try it in 10 seconds from any project directory where you ran Claude Code:

```bash
uvx handoff-verify check --latest
```

To wire it into Claude Code `Stop` and `SubagentStop` hooks:

```bash
uv tool install handoff-verify
handoff-verify init --write
handoff-verify doctor
```

Or install it directly as a Claude Code plugin (bundles `Stop` + `SubagentStop` hooks and the skill):

```bash
claude plugin marketplace add 1solomonwakhungu/handoff-verify
claude plugin install handoff-verify@handoff-verify
```

A few hook details that mattered a lot in real sessions:
- On `SubagentStop`, it reads only `agent_transcript_path` and never falls back to the parent session transcript.
- Claude Code sometimes fires `Stop` before flushing the last assistant message to the JSONL file. The hook compares against `last_assistant_message` in the hook payload, waits briefly for disk to catch up (only re-parsing if the file changes), and falls back to verifying the payload text against the tool evidence on disk.
- Silent on clean passes and warning-only passes, and loop-guarded via `stop_hook_active` so it will not block or continue the turn twice.
- Speed: 0.60 ms median on the 32-case eval suite, ~12.7 ms on a 1.1 MB transcript, ~57 ms total CLI wall time.

Start with the default `advise` mode rather than `block`. A finding means "the transcript does not show this", not necessarily that the claim is false (for example, if the agent inspected a file through a custom `python -c` script).

GitHub: https://github.com/1solomonwakhungu/handoff-verify

---

### Post 2: `r/codex`

**Title:**
handoff-verify: deterministic CLI to verify Codex CLI rollout transcripts (unread file citations, altered quotes, truncated searches)

**Body:**
If you use Codex CLI for code audits, refactors, or subagent reviews, you have probably seen a final report cite a line range that was never read, tweak a quoted snippet slightly, or claim "no other references in the repo" after a narrow or truncated `rg` call.

I built `handoff-verify`, a local Python 3.11+ CLI with zero runtime dependencies and zero LLM calls. It parses Codex CLI rollout logs (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`), Claude Code JSONL transcripts, and a documented generic JSONL format, then verifies every claim in the final assistant turn against what the tool calls actually returned.

What it parses in Codex rollouts:
- Modern `response_item` (`function_call` / `function_call_output`, `custom_tool_call`, `apply_patch`, `local_shell_call`, `event_msg`) and legacy flat logs.
- Shell-derived reads (`cat`, `nl`, `sed -n`, `head`, `tail`, `git show`, `git diff`), searches (`rg`, `grep`, `git grep`, `find`, `fd` including `| head` truncation and `--exclude-dir`), and edits (`apply_patch`, `sed -i`, redirects).

Usage on a Codex rollout:

```bash
uvx handoff-verify check ~/.codex/sessions/2026/11/24/rollout-....jsonl --repo .
```

You can also inspect what a rollout actually read, edited, searched, or truncated:

```bash
uvx handoff-verify inspect ~/.codex/sessions/2026/11/24/rollout-....jsonl
```

Output is one line on clean passes (`--quiet-on-pass` prints nothing), or structured text / Markdown / JSON findings with exact line ranges or search roots to re-check on exit `1`.

Benchmarks on macOS arm64 / Python 3.13.14:
- 32-case seeded eval suite (including Codex rollout cases): 100% precision (0 FP), 100% recall (16/16), 0.60 ms median.
- Throughput: 1.14 MB transcript in 12.7 ms, 5.76 MB transcript in 60.8 ms (~95 MB/s).

GitHub: https://github.com/1solomonwakhungu/handoff-verify

---

## X / Twitter Launch Thread

**Tweet 1:**
Coding agents often claim "no other callers anywhere" after a head_limit=5 grep, or cite file:line ranges they never read.

handoff-verify is a zero-LLM Python CLI + Claude Code hook that checks handoffs against transcripts.

uvx handoff-verify check --latest

**Tweet 2:**
Tool calls are already in Claude Code, Codex CLI, or generic JSONL logs.

handoff-verify checks:
1. path:line pointers (read windows, edit shifts, --repo)
2. Quoted code (altered or wrong-file)
3. Scope claims (truncated or narrow grep, unread cursors)

**Tweet 3:**
Excerpt of real output on a truncated grep claimed as "no other matches" (timing illustrative):

FAIL: 1 finding (1 error; 4 claims checked, 0.9ms)
[ERROR] TRUNCATED_SEARCH_OUTPUT: Grep#0 (result limit (5) reached)
Next step: Re-run without head_limit/truncation.

**Tweet 4:**
Measured on macOS arm64 / Python 3.13:
- 32-case seeded eval suite: 100% precision (0 FP), 100% recall (16/16), 0.60ms median
- 1.14 MB transcript: 12.7ms
- 5.76 MB transcript: 60.8ms (~95 MB/s)
- Full CLI run: ~57ms

Zero runtime dependencies (Python 3.11+ stdlib only).

**Tweet 5:**
Wire into Claude Code Stop/SubagentStop hooks (advise mode, loop-guarded via stop_hook_active):

uv tool install handoff-verify && handoff-verify init --write

Agent Skill: npx skills add 1solomonwakhungu/handoff-verify
Repo & plugin: https://github.com/1solomonwakhungu/handoff-verify

---

## GitHub Release Notes (`v0.1.1`)

### handoff-verify v0.1.1

Deterministic, zero-LLM verification of AI coding agent and subagent handoffs against tool-call transcripts. Pure Python 3.11+ standard library with zero third-party runtime dependencies.

#### Changes since initial PyPI `0.1.0`
- **Windows drive-letter path support:** Full normalization, repo-root detection, and grep content/quote parsing for Windows drive paths (`C:\repo\...`, both with and without line numbers) across Claude Code, Codex CLI, shell search outputs, and `doctor`.
- **Parser hardening:** Fixed falsy-zero `exitCode: 0` handling in Claude Code Bash `toolUseResult`, added fallback `FileEditEvent` for malformed `MultiEdit` entries, and capped orphan/malformed-argument `parse_warnings` in Claude Code and Codex parsers with suppression summaries.
- **Hook polling performance:** Skips redundant transcript re-parses during `last_assistant_message` wait polling when file size and mtime have not changed.
- **CLI & plugin polish:** Added `handoff-verify eval --json`, tilde expansion for `--repo`, env-prefix/tilde-aware executable resolution in `init --write`, Claude v2 empty plugin list handling in `doctor`, `python3.15` discovery in `bin/handoff-verify`, synchronized all 12 finding codes in `skills/handoff-verify/SKILL.md`, and added absolute GitHub URLs in `README.md` for PyPI.
- **CI & packaging:** Expanded CI matrix to Python 3.11, 3.12, and 3.13 with `contents: read` permissions, bumped GitHub Actions to `checkout@v7` and `setup-uv@v7`, added explicit `handoff-verify eval --verbose`, and added an isolated wheel build/install smoke-test job.

#### What it verifies
- **File pointers (`path:line`, `path:start-end`, `#L10-L20`):** checks transcript read coverage, `offset`/`limit` read windows, post-edit line shifts (`STALE_FILE_READ`), and live disk existence, line bounds, and content drift when `--repo` is provided (`UNREAD_FILE`, `LINE_OUT_OF_READ_WINDOW`, `FILE_NOT_FOUND`, `LINE_OUT_OF_BOUNDS`).
- **Verbatim code & output quotes:** checks inline and fenced snippets against observed file reads, edits, searches, and command outputs with whitespace/operator normalization (`QUOTE_NOT_FOUND`, `QUOTE_MISMATCH`).
- **Absence & completeness scope receipts:** verifies claims like *"no other callers"*, *"not used anywhere"*, and *"reviewed all pages"* against search roots, truncation limits, excluded directories, and unread pagination cursors (`NO_SEARCH_EVIDENCE`, `TRUNCATED_SEARCH_OUTPUT`, `NARROW_SEARCH_SCOPE`, `UNREAD_PAGINATION_CURSOR`, `EXCLUDED_SEARCH_DIRS`).

#### Benchmarks & evals (macOS arm64, Python 3.13.14)
- **Bundled 32-case eval suite (`handoff-verify eval`):** 100.0% precision (16 TP, 0 FP), 100.0% recall (16/16), 0.0% FPR, 100.0% code match, 0.60 ms median latency (1.38 ms p95, 1.56 ms max).
- **Scaling:** 115 KB in 2.50 ms, 1.14 MB in 12.74 ms, 5.76 MB in 60.77 ms (~95 MB/s). End-to-end CLI wall time: 56.8 ms median.

#### Quick install
```bash
uvx handoff-verify --version
uv tool install handoff-verify
pip install handoff-verify
```

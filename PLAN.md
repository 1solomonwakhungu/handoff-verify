# MVP Plan — handoff-verify

Build a production-quality, user-ready MVP across 4 sequenced/parallel PRs.

## PR 1 (Foundation — land first on `main`)
**Branch:** `feat/foundation-parsers`
- Create `pyproject.toml` (`handoff-verify`, Python `>=3.11`, pure stdlib runtime, `pytest` + `ruff` dev deps, `src/` layout).
- Define core data models in `src/handoff_verify/models.py`:
  - `ToolEvent`, `FileReadEvent`, `FileEditEvent`, `SearchEvent`, `CommandEvent`, `PaginationEvent`
  - `Claim` (`pointer`, `quote`, `absence`, `completeness`), `Finding` (`severity`, `code`, `claim`, `evidence`, `message`), `VerificationReport`
- Implement transcript parsers in `src/handoff_verify/parsers/`:
  - `claude_code.py`: parses real Claude Code JSONL transcripts (handles `Read`, `Grep`, `Glob`, `Bash`, `Edit`, `Write`, `WebFetch`, MCP tool calls, assistant text blocks, tool results, line ranges, truncation markers).
  - `codex.py`: parses Codex CLI JSONL session logs (function/tool calls & outputs, assistant messages).
  - `generic.py`: simple documented JSONL schema + auto-detection (`auto_parse(path)`).
- Implement CLI skeleton (`src/handoff_verify/cli.py`): `handoff-verify check <transcript>`, `handoff-verify inspect <transcript>`, `handoff-verify --version`.
- Unit tests covering all 3 parsers + auto-detection + malformed-line resilience.

## PR 2 (Deterministic Claim Extractors & Verifiers — parallel after PR 1)
**Branch:** `feat/verifiers`
- Implement claim extraction in `src/handoff_verify/extractors.py`:
  - File pointers: `path/to/file.py:42`, `path/to/file.py:10-25`, `file.py#L10-L20`, `file.py (line 42)`, backticked repo paths.
  - Quoted code snippets: inline backticked multi-token code snippets tied to a file/output and fenced code blocks claimed to come from a file or command.
  - Absence & completeness phrases: *"no other callers"*, *"no matches"*, *"not used anywhere"*, *"checked all"*, *"every file"*, *"reviewed all pages"*, *"all results"*.
- Implement verifiers in `src/handoff_verify/verifiers/`:
  - `pointers.py`: checks whether the cited file was actually read in the transcript, whether the cited line range was within the read window (`offset`/`limit`), and whether the file was subsequently modified (stale read detection via transcript edit events + optional live repo git hash/file line count check).
  - `quotes.py`: verifies quoted snippets against logged file reads and command stdout (exact + whitespace-normalized matching; flags fabricated or mismatched quotes).
  - `scope.py`: on absence/completeness claims, builds a factual scope receipt from the transcript's search/read/pagination events: searched roots vs repo root, excluded/ignored dirs, truncated search/command outputs (`truncated`, `limit reached`, capped results), and unread pagination cursors (`nextCursor`, `has_more`, `page 1 of N`).
- Wire verifiers into `handoff-verify check` with `--repo <path>`, `--strict`, `--format text|markdown|json`.
- Comprehensive unit & edge-case tests (avoiding false positives on prose examples, hypothetical code, or non-repo URLs).

## PR 3 (Hook Adapters, Formatters & Setup UX — parallel after PR 1)
**Branch:** `feat/hooks-and-ux`
- Implement clean formatters in `src/handoff_verify/formatters.py`:
  - Quiet-when-clean compact receipt (1 line or silent with `--quiet-on-pass`).
  - Loud, scannable Markdown anomaly receipt designed for both human readers AND parent agents (actionable next steps: exact unread lines or unsearched roots).
  - Machine-readable JSON output with stable schema version.
- Implement harness hook adapter in `src/handoff_verify/hooks.py`:
  - `handoff-verify hook claude-code`: reads Claude Code `SubagentStop` / `Stop` JSON payload from stdin, resolves `transcript_path` (or `agent_transcript_path` for subagents), runs verification, respects `stop_hook_active` to prevent infinite loops.
  - Supports `--mode advise` (exit 0, outputs structured feedback JSON `additionalContext` / `systemMessage`) and `--mode block` (exit 2 with concise stderr receipt on unverified claims, max 1 block via `stop_hook_active`).
  - Fail-open wrapper: internal errors log to stderr and exit 0 unless `--fail-closed` is passed.
- Implement `handoff-verify init --claude-code [--write]` to output or safely merge the hook snippet into `.claude/settings.json`.
- Integration tests simulating real Claude Code `Stop` and `SubagentStop` stdin JSON payloads.

## PR 4 (24-Case Seeded Eval Suite, E2E Polish & User Docs — final PR after PR 2 & PR 3)
**Branch:** `feat/evals-and-docs`
- Create `evals/cases/` with 24 realistic synthetic transcripts (12 clean sessions, 12 planted failure modes: unread `path:line`, out-of-window line number, stale read after edit, fabricated snippet quote, truncated grep claimed as "no matches", subdir-only grep claimed as repo-wide "no callers", unread page-2 cursor claimed as "reviewed all", Codex transcript failure, subagent fabricated claim, etc.).
- Add `handoff-verify eval` command (and pytest integration) that runs all 24 cases and reports precision, recall, false-positive rate, per-category breakdown, and median runtime in ms.
- Target metrics: **100% precision on clean cases (0 false positives)**, **>=90% recall on planted failures**, **<50ms median latency**.
- Finalize `README.md` with clear installation (`uvx`, `pip`), 60-second quickstart, Claude Code hook installation, real CLI output examples, JSON schema overview, and measured eval numbers.
- Ensure end-to-end smoke test via `uv run handoff-verify` passes cleanly.

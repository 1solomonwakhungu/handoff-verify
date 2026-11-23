# AGENTS.md — handoff-verify

## Mission
Build **handoff-verify**, a deterministic, zero-LLM CLI and hook tool that verifies claims made by AI coding agents and subagents against their actual tool-call transcripts (file pointers, verbatim code quotes, and search/pagination scope receipts).

## Tech Stack & Constraints
- **Language:** Python `>=3.11` managed with `uv`.
- **Runtime dependencies:** **Zero** third-party runtime dependencies (Python standard library only) so startup is <50ms and installs work cleanly across Python 3.11–3.15.
- **Dev dependencies:** `pytest`, `ruff`.
- **Package layout:** `src/handoff_verify/` with console entry point `handoff-verify = "handoff_verify.cli:main"`.
- **No LLM calls anywhere** in the runtime verification path. Everything is deterministic, local, and fast.

## Quality Bar
- `uv run pytest` passes 100% with zero skipped tests.
- `uv run ruff check .` and `uv run ruff format --check .` pass cleanly.
- Quiet when clean, loud on anomalies (never spam noisy warnings when all claims are verified).
- Fail-open option on hook integration so a hook crash never bricks a user's coding session.

## Mandatory Worker PR, Review & Merge Workflow
Every implementation worker thread owns its PR end-to-end (the orchestrator does NOT review or merge PRs):
1. Work in your assigned git worktree branched from `origin/main`.
2. Implement your milestone cleanly with thorough unit + integration tests.
3. Run `uv run pytest && uv run ruff check . && uv run ruff format --check .`.
4. Commit with clear commit messages, push to `origin`, open a PR targeting `main` via `gh pr create`, and call `link_pull_request` with the full PR URL.
5. **Mandatory Fable 5.2 Review:**
   - Call `orchestrator_capabilities` once in your thread before your first `delegate_task`.
   - Start a review delegate using `delegate_task` with:
     - `role: "review"`
     - `mode: "wait"` (or `"async"` and wait for wakeup)
     - `target: { "providerInstanceId": "claudeAgent_claude_proxy", "model": "claude-fable-5-2", "options": { "effort": "high" } }`
   - Pass the PR URL/number, `git diff origin/main...HEAD` (or instructions to inspect it), and test output. Ask Fable 5.2 for an adversarial review covering correctness, edge cases, false positives/negatives, CLI ergonomics, and test gaps.
   - Fix all valid findings, re-run tests & ruff, and push updates. If changes were non-trivial, run a fresh `delegate_task` re-check with Fable 5.2 (`claudeAgent_claude_proxy` / `claude-fable-5-2`) until approved with no blocking issues.
6. **CI & Self-Merge:**
   - Check PR checks (`gh pr view <num> --json statusCheckRollup`). If GitHub Actions CI is still running, call `watch_pull_request` and end your turn to wake on completion (do NOT use `sleep` or `gh --watch`).
   - If `origin/main` moved while you worked and there are conflicts, `git fetch origin && git rebase origin/main`, resolve conflicts, re-run tests, and push.
   - Once Fable 5.2 approves and CI is green, merge your PR yourself:
     `gh pr merge <num> --squash --delete-branch`
   - Report the merged PR URL, Fable 5.2 review summary, and final test results in your final message.

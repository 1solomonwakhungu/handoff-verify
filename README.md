# handoff-verify

**Deterministic, zero-LLM claim & scope verification for AI coding agents and subagents.**

Tools like `trustbut` and `ProofRun` verify test runs on code. `handoff-verify` verifies everything else an agent claims in its final report:

- **File pointers (`path:line`):** Did the agent actually read that file and line range on the current code state?
- **Verbatim quotes:** Does the quoted snippet actually appear in what the agent read?
- **Absence & completeness claims:** When a subagent says *"no other callers"* or *"reviewed all results"*, what roots did it actually search, what was truncated, and were pagination cursors left unread?

Zero LLM calls. Zero third-party runtime dependencies. Runs in milliseconds as a CLI or a `SubagentStop` / `Stop` hook.

*MVP currently under active build — see [PLAN.md](PLAN.md).*

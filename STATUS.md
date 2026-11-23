# STATUS

Last updated: 2026-11-23 (branch `dogfood/fable-usability`)

## Just completed: cold dogfood pass as an agent user

Ran every subcommand on 1,571 real Claude Code transcripts (972 main, 599 subagent) and
28 Codex rollouts from this machine, plus three live headless Claude Code 2.1.328 sessions
with the hook installed.

Shipped on this branch:

- **Hook race fix.** Claude Code fires `Stop` / `SubagentStop` before the final assistant
  message is reliably in the transcript file. The hook now reconciles the transcript with the
  payload's `last_assistant_message` (waits up to 1s, then verifies the payload text).
  Confirmed live: a fabricated subagent report that previously passed is now blocked.
- **Shell-read crediting.** Unified diff hunks credit new-side line numbers; `git show
  REV:path`, `git diff|blame|log -p -- path` and `awk ... file` count as reads; a range whose
  two endpoints were printed is grounded; a file-attributed quote printed by a command with
  no file attribution is accepted.
- **Stale-read false positives.** Shell edits in scratch copies outside the session tree and
  through unexpanded variables (`$D/a.py`) no longer mark repository files stale.
- **CLI.** `check --latest`, `--json`, `--format md`, `init` defaults to Claude Code, PATH
  warning on `init --write`, examples and exit codes in `--help`, clearer errors for a
  missing transcript, a directory, and a non-JSONL file.
- **Docs.** README install section now says what works today, quickstart starts with
  `check --latest`, new "How it behaves on real sessions" section with measured numbers, new
  `skills/handoff-verify/SKILL.md`.

## Key results

| Metric | Before | After |
| --- | ---: | ---: |
| Real transcripts with an error-level finding (of 1,571) | 512 (32.6%) | 343 (21.8%) |
| `STALE_FILE_READ` errors | 112 | 22 |
| `QUOTE_MISMATCH` errors | 120 | 27 |
| `UNREAD_FILE` errors | 974 | 474 |
| Seeded eval suite | 24/24 | 24/24 |
| Tests | 170 pass | 193 pass |
| Latency on real transcripts | p50 89 ms, max 1.2 s (52 MB file) | unchanged |

## What's next (ordered by adoption impact)

1. **Publish to PyPI.** `uvx handoff-verify` fails today and the repository is private, so
   neither README install path worked for an outside user.
2. **Bring the real-session error rate down before recommending `block` mode.** Largest
   remaining sources, from sampling:
   - output redirected to a log and read back (`sed -n 1,40p a.py > /tmp/x.log`, then Read)
   - `grep -n` hits adjacent to the cited line; several commands joined with `;`
   - parent agents relaying subagent `path:line` citations under a different path spelling
   - bare mentions of files produced by scripts (`UNREAD_FILE` without a line number)
   - negated mentions ("`src/app.py:99` does not exist") flagged as citations
   - `no usage` / `no open issue` matched as absence claims about code
3. **Hold out a real-transcript corpus as an eval.** The seeded suite scores 100% while real
   sessions fail one time in five; the suite cannot see this class of regression.
4. **Stay quiet on warning-only reports in the hook.** 173 real transcripts pass with
   warnings only and would still print a `systemMessage` on stop.
5. **Make fail-open visible.** Fail-open exits 0 with one stderr line, which Claude Code does
   not show. Exit 1 is non-blocking in Claude Code and surfaces the line. Usage errors on
   `hook` (a typo'd `--mode`) are swallowed the same way.
6. **Package as a Claude Code plugin** (`.claude-plugin/plugin.json`, `hooks/hooks.json`, the
   skill) once the CLI is installable with `uvx`.
7. **More harnesses.** Codex gets `check` only; there is no hook adapter for Codex, Cursor or
   OpenCode, and `--latest` only knows Claude Code's layout.

## Notes for the next session

- Corpus scripts and raw results are in `/tmp/hv/` on the machine that ran this pass (not
  committed; they contain private transcript paths).
- `AGENTS.md` describes a self-merge workflow. This branch was opened as a PR for review and
  not merged.

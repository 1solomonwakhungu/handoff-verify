---
name: handoff-verify
description: Check that the file pointers, code quotes and "no other callers"-style claims in an agent's final report are backed by what the agent actually read and searched. Use before handing work back, when a handoff-verify hook reports findings, or when reviewing a subagent's report.
---

# handoff-verify

`handoff-verify` reads an agent transcript and checks the final message against the tool
calls in it. It makes no model calls and runs in well under a second.

It checks three kinds of claim:

- **Pointers**: `src/auth.py:42`, `src/auth.py:40-55`, `auth.py#L10-L20`. Was that file read,
  and were those lines in what was shown?
- **Quotes**: inline or fenced code attributed to a file or to command output. Does that exact
  text appear in what was read, written or printed?
- **Absence and completeness**: "no other callers", "not used anywhere", "reviewed all pages".
  Was there an untruncated search over that scope, and were all pagination cursors followed?

## When to run it

- Before reporting back from a review, an investigation or any task whose report cites code.
- When a `Stop` or `SubagentStop` hook returns a `handoff-verify: FAIL` message.
- When you receive a subagent's report and plan to repeat its citations.

## Commands

```bash
# Check the current Claude Code session (run from the project directory)
handoff-verify check --latest --json --quiet-on-pass

# Check a specific transcript, such as a subagent's
handoff-verify check <path-to-transcript.jsonl> --json

# See what a transcript's agent actually read, edited and searched
handoff-verify inspect <path-to-transcript.jsonl>

# Check PATH, Python version, and Claude Code hook configuration
handoff-verify doctor
```

Exit codes: `0` passed, `1` findings, `2` usage or parse error. With `--quiet-on-pass`, a
clean check prints nothing.

Transcript locations:

- Claude Code: `~/.claude/projects/<cwd-with-dashes>/<session-id>.jsonl`
- Claude Code subagent: `.../<session-id>/subagents/agent-<id>.jsonl`
- Codex CLI: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`

## Reading a finding

Each entry in `findings` has:

- `code`: what went wrong (table below)
- `claim.text` and `claim.context`: the exact words in the report
- `evidence`: what the transcript does and does not show
- `suggestion`: the read or search that would ground the claim

A finding means the transcript does not show support for the claim. It does not prove the
claim false. Resolve it by looking, then either keep the claim or correct it.

| Code | What to do |
| --- | --- |
| `UNREAD_FILE` | Read the cited lines. If they say what you claimed, keep the citation; otherwise fix or drop it. |
| `LINE_OUT_OF_READ_WINDOW` | You read the file but not those lines. Read them. |
| `STALE_FILE_READ` | The file changed after you read it. Re-read and update the line numbers. |
| `FILE_NOT_FOUND`, `LINE_OUT_OF_BOUNDS` | The path or line does not exist on disk. Correct it. |
| `QUOTE_NOT_FOUND` | The quoted text was never observed. Re-read and quote exactly, or paraphrase without code formatting. |
| `QUOTE_MISMATCH` | The quote was seen in a different file. Fix the attribution. |
| `NO_SEARCH_EVIDENCE` | You claimed absence without searching. Run the search or soften the claim. |
| `TRUNCATED_SEARCH_OUTPUT` | The search output was cut off (`head`, a result limit). Re-run it unbounded. |
| `NARROW_SEARCH_SCOPE` | You searched a subdirectory and claimed the whole repository. Widen the search or name the scope you searched. |
| `UNREAD_PAGINATION_CURSOR` | A tool returned more pages than you fetched. Fetch them or say how many you reviewed. |
| `EXCLUDED_SEARCH_DIRS` | The search excluded directories the claim does not mention. Name them or search them. |

## Habits that keep reports clean

- Cite a line only after reading it with a tool that shows line numbers: the Read tool,
  `grep -n`, `cat -n`, `sed -n 'A,Bp' file` as a single command, or `git diff`.
- Quote code by copying it from tool output. If you are summarising, do not use backticks.
- State the scope of a search in the claim: "no other callers under `src/`".
- After editing a file, re-read the region before citing line numbers in it.

## Limits

- Reads through arbitrary scripts (`python - <<EOF` slicing files) cannot always be mapped to
  line numbers and may still be reported as unread. One direct read of the cited lines clears
  the finding.
- It verifies that claims are grounded in the transcript. It does not judge whether the
  conclusions drawn from them are right.

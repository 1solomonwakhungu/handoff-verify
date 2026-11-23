"""Regression tests for all Fable 5.2 adversarial review findings (B1-B15 & NB1-NB9)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from handoff_verify.extractors import extract_claims, extract_pointer_claims
from handoff_verify.models import (
    AssistantMessage,
    CommandEvent,
    FileEditEvent,
    FileReadEvent,
    PaginationEvent,
    ParsedTranscript,
    SearchEvent,
    ToolEvent,
)
from handoff_verify.parsers import auto_parse
from handoff_verify.pipeline import verify_transcript


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


# --- B1: Edit matrix (above, inside, below shifted, sequential, cat -n, grep) ---


def test_b1_edit_matrix_clean_and_stale() -> None:
    file_lines = "\n".join(f"line{i} = {i}" for i in range(1, 51))
    base_read = FileReadEvent(
        event_index=0,
        tool_name="Read",
        file_path="src/a.py",
        start_line=1,
        end_line=50,
        line_count=50,
        content=file_lines,
    )
    expanding_edit = FileEditEvent(
        event_index=1,
        tool_name="Edit",
        file_path="src/a.py",
        operation="edit",
        old_text="line10 = 10",
        new_text="if x is None:\n    return None\nline10 = 10",
    )

    # 1. Claim inside newly added lines 10-11 -> CLEAN
    tr_inside = ParsedTranscript(
        cwd="/repo",
        file_reads=[base_read],
        file_edits=[expanding_edit],
        assistant_messages=[
            AssistantMessage(index=0, text="Added a None guard at `src/a.py:10-11`.", is_final=True)
        ],
    )
    assert verify_transcript(tr_inside).findings == []

    # 2. Untouched line strictly ABOVE the edit (line 5 < 10) -> CLEAN
    tr_above = ParsedTranscript(
        cwd="/repo",
        file_reads=[base_read],
        file_edits=[expanding_edit],
        assistant_messages=[
            AssistantMessage(index=0, text="Config is loaded at `src/a.py:5`.", is_final=True)
        ],
    )
    assert verify_transcript(tr_above).findings == []

    # 3. Shifted line strictly BELOW the expanding edit (line 42 > 12) -> STALE_FILE_READ error
    tr_below = ParsedTranscript(
        cwd="/repo",
        file_reads=[base_read],
        file_edits=[expanding_edit],
        assistant_messages=[
            AssistantMessage(index=0, text="Validation runs at `src/a.py:42`.", is_final=True)
        ],
    )
    rep_below = verify_transcript(tr_below)
    assert rep_below.passed is False
    assert any(f.code == "STALE_FILE_READ" and f.severity == "error" for f in rep_below.findings)

    # 4. Same-line-count edit phrased without an edit verb ("`src/a.py:10` now uses ...") -> CLEAN
    same_count_edit = FileEditEvent(
        event_index=1,
        tool_name="Edit",
        file_path="src/a.py",
        operation="edit",
        old_text="line10 = 10",
        new_text="line10 = 99",
    )
    tr_same = ParsedTranscript(
        cwd="/repo",
        file_reads=[base_read],
        file_edits=[same_count_edit],
        assistant_messages=[
            AssistantMessage(index=0, text="`src/a.py:10` now uses 99.", is_final=True)
        ],
    )
    assert verify_transcript(tr_same).findings == []

    # 5. Two sequential edits -> citing the FIRST edit's range is still CLEAN
    edit_2 = FileEditEvent(
        event_index=2,
        tool_name="Edit",
        file_path="src/a.py",
        operation="edit",
        old_text="line30 = 30",
        new_text="line30 = 30\nline30b = 31",
    )
    tr_seq = ParsedTranscript(
        cwd="/repo",
        file_reads=[base_read],
        file_edits=[expanding_edit, edit_2],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Guard at `src/a.py:10-12` and extra line at `src/a.py:32-33`.",
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr_seq).findings == []

    # 6. Edit result carries `cat -n` snippet covering line 42 -> CLEAN
    edit_with_snippet = FileEditEvent(
        event_index=1,
        tool_name="Edit",
        file_path="src/a.py",
        operation="edit",
        old_text="line10 = 10",
        new_text="a = 1\nb = 2\nc = 3",
        raw_output="    41→line39 = 39\n    42→line40 = 40\n    43→line41 = 41",
    )
    tr_snip = ParsedTranscript(
        cwd="/repo",
        file_reads=[base_read],
        file_edits=[edit_with_snippet],
        assistant_messages=[
            AssistantMessage(index=0, text="Confirmed `src/a.py:42`.", is_final=True)
        ],
    )
    assert verify_transcript(tr_snip).findings == []

    # 7. Post-edit Grep -n hit covering line 44 -> CLEAN
    tr_post_grep = ParsedTranscript(
        cwd="/repo",
        file_reads=[base_read],
        file_edits=[expanding_edit],
        searches=[
            SearchEvent(
                event_index=2,
                tool_name="Grep",
                query="line42",
                matched_files=["src/a.py"],
                output="src/a.py:44:line42 = 42",
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Confirmed `src/a.py:44`.", is_final=True)
        ],
    )
    assert verify_transcript(tr_post_grep).findings == []


def test_nb7_same_event_index_bash_read_and_sed_flags_stale() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Bash",
                file_path="src/a.py",
                start_line=1,
                end_line=20,
                line_count=20,
            )
        ],
        file_edits=[
            FileEditEvent(
                event_index=0,
                tool_name="Bash",
                file_path="src/a.py",
                operation="bash_modify",
            )
        ],
        assistant_messages=[AssistantMessage(index=0, text="See `src/a.py:15`.", is_final=True)],
    )
    rep = verify_transcript(tr)
    assert rep.passed is False
    assert any(f.code == "STALE_FILE_READ" for f in rep.findings)


# --- B2: Monorepo & subdir-cwd relative suffix matching ---


def test_b2_relative_suffix_matching_both_directions() -> None:
    tr_mono = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="/repo/apps/api/src/handlers/auth.ts",
                start_line=1,
                end_line=30,
                line_count=30,
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Checked `src/handlers/auth.ts:12`.", is_final=True)
        ],
    )
    assert verify_transcript(tr_mono).findings == []

    tr_subcwd = ParsedTranscript(
        cwd="/repo/packages/web",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="/repo/packages/web/src/a.py",
                start_line=1,
                end_line=20,
                line_count=20,
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Checked `packages/web/src/a.py:3`.", is_final=True)
        ],
    )
    assert verify_transcript(tr_subcwd).findings == []


# --- B3: Special path chars ([id], (auth), +page, $id) ---


def test_b3_framework_route_paths_with_special_chars() -> None:
    paths = [
        "app/[id]/page.tsx",
        "app/(auth)/login/page.tsx",
        "src/routes/+page.svelte",
        "app/routes/$id.tsx",
    ]
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=i,
                tool_name="Read",
                file_path=p,
                start_line=1,
                end_line=30,
                line_count=30,
            )
            for i, p in enumerate(paths)
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "Checked `app/[id]/page.tsx:12`, `app/(auth)/login/page.tsx:12`, "
                    "`src/routes/+page.svelte:12`, and app/routes/$id.tsx:12."
                ),
                is_final=True,
            )
        ],
    )
    claims = extract_claims(tr)
    extracted = {c.file_path for c in claims if c.kind == "pointer"}
    assert extracted == set(paths)
    assert verify_transcript(tr).findings == []


# --- B4: Dotted identifiers and domains never trigger UNREAD_FILE ---


def test_b4_dotted_identifiers_not_flagged_as_unread_files() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "Uses `console.log` and reads `process.env`; `docs.rs` hosts the docs "
                    "and `app.config` holds settings, while `res.json` serializes output."
                ),
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr).findings == []


# --- B5: Command args, ls dir joining, ruff/subagent path:line, negated-read, next-step ---


def test_b5_non_read_evidence_and_honest_disclosures_are_clean() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0, tool_name="Read", file_path="src/a.py", start_line=1, end_line=20
            )
        ],
        commands=[
            CommandEvent(
                event_index=1,
                tool_name="Bash",
                command="pytest tests/test_auth.py -q",
                stdout="5 passed in 0.1s",
                exit_code=0,
            ),
            CommandEvent(
                event_index=2,
                tool_name="Bash",
                command="python eval.py --out results/eval.json",
                stdout="Done.",
                exit_code=0,
            ),
            CommandEvent(
                event_index=3,
                tool_name="Bash",
                command="ls src/",
                stdout="a.py\nb.py\n",
                exit_code=0,
            ),
            CommandEvent(
                event_index=4,
                tool_name="Bash",
                command="ruff check src/a.py",
                stdout="src/a.py:10:5: E501 Line too long",
                exit_code=1,
                is_error=True,
            ),
        ],
        events=[
            ToolEvent(
                event_index=5,
                tool_name="Task",
                raw_output="Subagent found the handler at src/sub.py:42: def handle():",
                has_result=True,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "All 5 tests in `tests/test_auth.py` pass. "
                    "Wrote results to `results/eval.json`. "
                    "The package contains `src/a.py` and `src/b.py`. "
                    "Ruff flagged `src/a.py:10`. "
                    "Subagent reported `src/sub.py:42`. "
                    "I reviewed `src/a.py` but did not read `src/big.py`. "
                    "Next step: update `CHANGELOG.md`."
                ),
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr).findings == []


# --- B6: Summarizing a deletion is clean (with and without --repo) ---


def test_b6_deletion_summary_clean_with_and_without_repo(tmp_path: Path) -> None:
    tr = ParsedTranscript(
        cwd=str(tmp_path),
        file_reads=[
            FileReadEvent(
                event_index=0, tool_name="Read", file_path="src/a.py", start_line=1, end_line=10
            )
        ],
        file_edits=[
            FileEditEvent(event_index=1, tool_name="Bash", file_path="src/a.py", operation="delete")
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Removed `src/a.py` since it was unused.", is_final=True)
        ],
    )
    assert verify_transcript(tr).findings == []
    assert verify_transcript(tr, repo_root=tmp_path).findings == []


# --- B7: Quote precision (cmds, returns, op spaces, contrast, err tools, diffs, subseq) ---


def test_b7_quote_clean_cases_zero_false_positives() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/a.py",
                start_line=1,
                end_line=10,
                content="def parse_config(path: Path):\n    timeout = 30\n    return {}\n",
            ),
            FileReadEvent(
                event_index=1,
                tool_name="Read",
                file_path="src/b.py",
                start_line=1,
                end_line=5,
                content="async def run(client, url):\n    return await client.fetch(url)\n",
            ),
        ],
        commands=[
            CommandEvent(
                event_index=2,
                tool_name="Bash",
                command="npx tsc --noEmit",
                stdout="0 errors",
                exit_code=0,
            ),
            CommandEvent(
                event_index=3,
                tool_name="Bash",
                command="CI=1 pnpm test",
                stdout="line1: starting\nline2: running\nline3: 12 passing\nline4: done\n",
                exit_code=0,
            ),
            CommandEvent(
                event_index=4,
                tool_name="Bash",
                command="tox -e py311",
                stdout="congratulations",
                exit_code=0,
            ),
        ],
        events=[
            ToolEvent(
                event_index=5,
                tool_name="WebFetch",
                raw_output="HTTP Error: 403 Forbidden: rate limit exceeded",
                is_error=True,
                has_result=True,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "`npx tsc --noEmit` reports no errors. "
                    "`CI=1 pnpm test` output shows 12 passing. "
                    "`tox -e py311` returned clean. "
                    "`parse_config(path)` returns a dict. "
                    "`src/a.py:2` sets `timeout=30`. "
                    "Unlike `src/a.py:2`, the new module uses `await client.fetch(url)`. "
                    "WebFetch failed with `403 Forbidden: rate limit exceeded`.\n\n"
                    "Next step — add to `src/a.py`:\n"
                    "```python\n"
                    "def new_helper(x):\n"
                    "    return x + 1\n"
                    "```\n\n"
                    "I changed `src/a.py`:\n"
                    "```\n"
                    "- timeout = 10\n"
                    "+ timeout = 30\n"
                    "```\n\n"
                    "The command printed:\n"
                    "```\n"
                    "line1: starting\n"
                    "line3: 12 passing\n"
                    "```\n"
                ),
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr).findings == []


def test_nb3_quote_boundary_and_removed_code_caught() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/a.py",
                start_line=1,
                end_line=5,
                content="timeout = 30\nold_fn(1, 2)\n",
            )
        ],
        file_edits=[
            FileEditEvent(
                event_index=1,
                tool_name="Edit",
                file_path="src/a.py",
                operation="edit",
                old_text="old_fn(1, 2)",
                new_text="new_fn(1, 2)",
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="In `src/a.py:1`, it sets `out = 30` and calls `old_fn(1, 2)`.",
                is_final=True,
            )
        ],
    )
    rep = verify_transcript(tr)
    assert rep.passed is False
    quote_findings = [f for f in rep.findings if f.code == "QUOTE_NOT_FOUND"]
    assert len(quote_findings) == 2


# --- B8: Ordinary prose must NOT trigger NO_SEARCH_EVIDENCE ---


def test_b8_ordinary_prose_and_gh_issue_list_are_clean() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0, tool_name="Read", file_path="src/a.py", start_line=1, end_line=10
            )
        ],
        commands=[
            CommandEvent(
                event_index=1,
                tool_name="Bash",
                command="gh issue list --state open",
                stdout="[]",
                exit_code=0,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "No other files needed changes. "
                    "All 29 files are already formatted. "
                    "I committed all 3 files. "
                    "There are no open issues with this approach. "
                    "No results are reused between runs. "
                    "No remaining items on the checklist. "
                    "No matches for the old warning in the log. "
                    "`gh issue list` shows there are no open issues."
                ),
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr).findings == []


# --- B9: Honestly scoped claims with search verbs or post-match hedging ---


def test_b9_honestly_scoped_claims_are_clean() -> None:
    search = SearchEvent(
        event_index=0,
        tool_name="Grep",
        query="legacy_fn",
        search_path="src/auth",
        search_paths=["src/auth"],
        match_count=0,
    )
    for text in (
        "I searched `src/auth/` and found no other callers.",
        "A grep of src/auth shows no other callers.",
        "No other callers turned up, but I only searched `src/auth/`, so treat this as partial.",
    ):
        tr = ParsedTranscript(
            cwd="/repo",
            searches=[search],
            assistant_messages=[AssistantMessage(index=0, text=text, is_final=True)],
        )
        assert verify_transcript(tr).findings == [], f"Unexpected findings for: {text}"


# --- B10: Unrelated truncated search and unrelated unread cursor must NOT fail clean claims ---


def test_b10_unrelated_truncation_and_cursor_do_not_fail_clean_claim() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                query="import",
                search_path=".",
                truncated=True,
                truncation_reason="result limit (2) reached",
            ),
            SearchEvent(
                event_index=1,
                tool_name="Grep",
                query="legacy_auth",
                search_path=".",
                truncated=False,
                match_count=0,
            ),
        ],
        paginations=[
            PaginationEvent(
                event_index=2,
                tool_name="mcp__github__list_issues",
                resource="issues",
                page=1,
                total_pages=3,
                has_more=True,
                next_cursor="cur_2",
                cursor_followed=False,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Grep for `legacy_auth` returned no results anywhere in the repo.",
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr).findings == []


# --- B11: Repo-wide wording overrides `in path`; windowed Read fails file-scoped absence ---


def test_b11_planted_repo_wide_override_and_windowed_read_caught() -> None:
    sub_search = SearchEvent(
        event_index=0,
        tool_name="Grep",
        query="legacy_auth",
        search_path="src/auth",
        search_paths=["src/auth"],
        match_count=0,
    )
    for text in (
        "There are no other callers of `legacy_auth` anywhere in the repo "
        "besides the one in `src/a.py`.",
        "No other callers of `legacy_auth` anywhere in the codebase; "
        "it is only called in `src/auth/`.",
    ):
        tr = ParsedTranscript(
            cwd="/repo",
            file_reads=[
                FileReadEvent(
                    event_index=1, tool_name="Read", file_path="src/a.py", start_line=1, end_line=10
                )
            ],
            searches=[sub_search],
            assistant_messages=[AssistantMessage(index=0, text=text, is_final=True)],
        )
        rep = verify_transcript(tr)
        assert rep.passed is False, f"Expected failure for: {text}"
        assert any(f.code == "NARROW_SEARCH_SCOPE" for f in rep.findings)

    # 5-line windowed Read + "There are no TODOs in `src/a.py`" -> FAIL
    tr_windowed = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/a.py",
                start_line=1,
                end_line=5,
                offset=1,
                limit=5,
                line_count=5,
                truncated=False,
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="There are no TODOs in `src/a.py`.", is_final=True)
        ],
    )
    rep_win = verify_transcript(tr_windowed)
    assert rep_win.passed is False
    assert any(f.code == "TRUNCATED_SEARCH_OUTPUT" for f in rep_win.findings)


# --- B12: Query-matched scope check & Glob-only cannot satisfy content-absence claim ---


def test_b12_unrelated_root_grep_and_glob_only_do_not_bypass() -> None:
    # Root grep for `import os` + subdir grep for `legacy_auth` -> NARROW_SEARCH_SCOPE
    tr_mixed = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(event_index=0, tool_name="Grep", query="import os", search_path="."),
            SearchEvent(
                event_index=1,
                tool_name="Grep",
                query="legacy_auth",
                search_path="src/auth",
                search_paths=["src/auth"],
            ),
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="There are no other callers of `legacy_auth` anywhere in the repo.",
                is_final=True,
            )
        ],
    )
    rep_mixed = verify_transcript(tr_mixed)
    assert rep_mixed.passed is False
    assert any(f.code == "NARROW_SEARCH_SCOPE" for f in rep_mixed.findings)

    # Root Glob only -> NO_SEARCH_EVIDENCE
    tr_glob = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(event_index=0, tool_name="Glob", query="**/*.py", search_path="."),
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="There are no other callers of `legacy_auth` anywhere in the repo.",
                is_final=True,
            )
        ],
    )
    rep_glob = verify_transcript(tr_glob)
    assert rep_glob.passed is False
    assert any(f.code == "NO_SEARCH_EVIDENCE" for f in rep_glob.findings)


# --- B13: Summary followed by trailing TodoWrite bookkeeping tool is STILL verified ---


def test_b13_summary_before_trailing_todowrite_is_verified(tmp_path: Path) -> None:
    transcript_file = _write_jsonl(
        tmp_path / "split_todowrite.jsonl",
        [
            {
                "type": "assistant",
                "cwd": "/repo",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "The bug is at `src/never_read.py:42` and there are "
                                "no other callers anywhere in the repo."
                            ),
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "cwd": "/repo",
                "message": {
                    "id": "m2",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t_todo",
                            "name": "TodoWrite",
                            "input": {"todos": [{"content": "done", "status": "completed"}]},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "cwd": "/repo",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t_todo", "content": "ok"}],
                },
            },
            {
                "type": "assistant",
                "cwd": "/repo",
                "message": {
                    "id": "m3",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "All tasks are complete."}],
                },
            },
        ],
    )
    parsed = auto_parse(transcript_file)
    rep = verify_transcript(parsed)
    assert rep.passed is False
    codes = {f.code for f in rep.findings}
    assert "UNREAD_FILE" in codes
    assert "NO_SEARCH_EVIDENCE" in codes


# --- B14: Colon-terminated cue alternatives match at end-of-line ---


def test_b14_colon_terminated_cues_catch_fabricated_fenced_output() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        commands=[
            CommandEvent(
                event_index=0,
                tool_name="Bash",
                command="pytest",
                stdout="3 failed, 2 passed",
                exit_code=1,
                is_error=True,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Test output:\n```\n82 passed in 0.20s\n```\n",
                is_final=True,
            )
        ],
    )
    rep = verify_transcript(tr)
    assert rep.passed is False
    assert any(f.code == "QUOTE_NOT_FOUND" for f in rep.findings)


# --- B15 + NB1 + NB2: Latency & ReDoS regression tests ---


def test_b15_latency_on_large_grep_output_and_many_reads() -> None:
    grep_lines = "\n".join(
        f"src/mod_{i % 50}.py:{(i % 200) + 1}:def fn_{i}(): pass" for i in range(10_000)
    )
    reads = [
        FileReadEvent(
            event_index=i,
            tool_name="Read",
            file_path=f"src/mod_{i}.py",
            start_line=1,
            end_line=200,
            line_count=200,
            content="\n".join(f"x_{j} = {j}" for j in range(200)),
        )
        for i in range(50)
    ]
    text = " ".join(f"`src/mod_{i}.py:10`" for i in range(40)) + " calls `x_10 = 10`."
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=reads,
        searches=[SearchEvent(event_index=51, tool_name="Grep", query="fn", output=grep_lines)],
        assistant_messages=[AssistantMessage(index=0, text=text, is_final=True)],
    )
    t0 = time.perf_counter()
    rep = verify_transcript(tr)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert rep.passed is True
    assert elapsed_ms < 150.0


def test_nb1_no_redos_on_huge_unbroken_tokens() -> None:
    huge_dots = "a." * 10_000 + "py:42"
    huge_slashes = "ab/" * 10_000 + "file.py:42"
    t0 = time.perf_counter()
    assert extract_pointer_claims(huge_dots) == []
    assert extract_pointer_claims(huge_slashes) == []
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 50.0


# --- NB8 & NB9: Repo confinement and clause-bounded non-existence ---


def test_nb8_live_repo_check_confined_inside_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET = 'top-secret'\n" * 10, encoding="utf-8")

    tr = ParsedTranscript(
        cwd=str(repo),
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="../secret.py",
                start_line=1,
                end_line=10,
                line_count=10,
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Checked `../secret.py:5`.", is_final=True)
        ],
    )
    # Must NOT leak existence or line count of files outside repo_root
    rep = verify_transcript(tr, repo_root=repo)
    assert not any(f.code in ("FILE_NOT_FOUND", "LINE_OUT_OF_BOUNDS") for f in rep.findings)


def test_nb9_nonexistence_in_next_clause_does_not_suppress_file_pointer(tmp_path: Path) -> None:
    tr = ParsedTranscript(
        cwd=str(tmp_path),
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="`src/ghost.py` validates tokens; missing ones are not found errors.",
                is_final=True,
            )
        ],
    )
    rep = verify_transcript(tr, repo_root=tmp_path)
    assert rep.passed is False
    codes = {f.code for f in rep.findings}
    assert "UNREAD_FILE" in codes
    assert "FILE_NOT_FOUND" in codes


# --- Round 2 Re-Review Regression Tests (R2-1 through R2-10 + NB) ---


def test_r2_1_partial_reread_after_edit_keeps_unshifted_lines_valid() -> None:
    lines = "\n".join(f"line{i} = {i}" for i in range(1, 201))
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/a.py",
                start_line=1,
                end_line=200,
                line_count=200,
                content=lines,
            ),
            FileReadEvent(
                event_index=2,
                tool_name="Read",
                file_path="src/a.py",
                start_line=140,
                end_line=160,
                line_count=21,
                content="\n".join(f"line{i} = {i}" for i in range(140, 161)),
            ),
        ],
        file_edits=[
            FileEditEvent(
                event_index=1,
                tool_name="Edit",
                file_path="src/a.py",
                operation="edit",
                old_text="line150 = 150",
                new_text="a = 1\nb = 2\nc = 3\nline150 = 150",
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Config is defined at `src/a.py:20`.", is_final=True)
        ],
    )
    assert verify_transcript(tr).findings == []

    # Shifted line BELOW edit (line 180) and NOT in post-edit re-read 140-160 MUST still fail!
    tr_stale = ParsedTranscript(
        cwd="/repo",
        file_reads=tr.file_reads,
        file_edits=tr.file_edits,
        assistant_messages=[
            AssistantMessage(index=0, text="Helper runs at `src/a.py:180`.", is_final=True)
        ],
    )
    rep_stale = verify_transcript(tr_stale)
    assert rep_stale.passed is False
    assert any(f.code == "STALE_FILE_READ" for f in rep_stale.findings)


def test_r2_2_changed_from_old_to_new_and_arrow_quotes_are_clean() -> None:
    read = FileReadEvent(
        event_index=0,
        tool_name="Read",
        file_path="src/auth.py",
        start_line=1,
        end_line=10,
        content="timeout = 30\n",
    )
    edit = FileEditEvent(
        event_index=1,
        tool_name="Edit",
        file_path="src/auth.py",
        operation="edit",
        old_text="timeout = 30",
        new_text="timeout = 45",
    )
    for text in (
        "Changed `src/auth.py:1` from `timeout = 30` to `timeout = 45`.",
        "Updated `src/auth.py:1`: `timeout = 30` → `timeout = 45`.",
        "Updated `src/auth.py:1`: `timeout = 30` -> `timeout = 45`.",
        "In `src/auth.py:1`, `timeout = 30` is now `timeout = 45`.",
        "Bumped `src/auth.py:1` from `timeout = 30` to `timeout = 45`.",
    ):
        tr = ParsedTranscript(
            cwd="/repo",
            file_reads=[read],
            file_edits=[edit],
            assistant_messages=[AssistantMessage(index=0, text=text, is_final=True)],
        )
        assert verify_transcript(tr).findings == [], f"Failed for: {text}"


def test_r2_3_scoped_anywhere_in_dir_or_file_is_clean() -> None:
    tr_dir = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Grep",
                query="legacy_auth",
                search_path="src/auth",
                search_paths=["src/auth"],
                match_count=0,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="There are no other usages of `legacy_auth` anywhere in `src/auth/`.",
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr_dir).findings == []

    tr_file = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/auth/a.py",
                start_line=1,
                end_line=20,
                line_count=20,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="`legacy_auth` isn't used anywhere else in `src/auth/a.py`.",
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr_file).findings == []


def test_r2_4_ordinary_closing_prose_produces_zero_findings() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0, tool_name="Read", file_path="src/a.py", start_line=1, end_line=10
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "All tests pass and there are no remaining issues. "
                    "No further issues found. "
                    "No remaining TODOs. "
                    "I read all 3 files you listed and summarized them. "
                    "The function has no references to global state. "
                    "There are no instances where this can return None. "
                    "This endpoint has no consumers yet."
                ),
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr).findings == []


def test_r2_5_signature_and_call_shorthands_are_clean() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/auth.py",
                start_line=1,
                end_line=10,
                content=(
                    "def validate_token(token: str, *, strict: bool = False) -> dict[str, Any]:\n"
                    "    data = load(path, strict=strict, encoding='utf-8')\n"
                    "    return check(token, data)\n"
                ),
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "`validate_token(token)` in `src/auth.py:1` checks the token and returns "
                    "`dict[str, Any]`. It calls `load(path, strict)` with `strict=True` and "
                    "uses `check(token)`."
                ),
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr).findings == []


def test_r2_6_fenced_usage_example_after_file_mention_is_clean() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="src/cli.py",
                start_line=1,
                end_line=10,
                content="def main():\n    pass\n",
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "`src/cli.py` now supports a `--json` flag:\n"
                    "```bash\n"
                    "handoff-verify check session.jsonl --json\n"
                    "```\n\n"
                    "`src/cli.py` emits structured output:\n"
                    "```json\n"
                    '{"passed": true, "findings": []}\n'
                    "```\n"
                ),
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr).findings == []


def test_r2_7_single_file_grep_no_linenum_grep_and_git_diff_credited() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Bash:grep",
                query="verify_token",
                search_path="src/a.py",
                search_paths=["src/a.py"],
                output="42:    return verify_token(user, secret)",
            ),
            SearchEvent(
                event_index=1,
                tool_name="Bash:grep",
                query="timeout",
                search_path=".",
                output="src/auth.py:    timeout = 30",
            ),
        ],
        commands=[
            CommandEvent(
                event_index=2,
                tool_name="Bash",
                command="git diff",
                stdout=(
                    "diff --git a/src/diffed.py b/src/diffed.py\n"
                    "--- a/src/diffed.py\n"
                    "+++ b/src/diffed.py\n"
                    "@@ -1,2 +1,3 @@\n"
                    " def run():\n"
                    "+    return compute_result(1, 2)\n"
                ),
                exit_code=0,
            )
        ],
        file_reads=[
            FileReadEvent(
                event_index=3,
                tool_name="Read",
                file_path="src/auth.py",
                start_line=1,
                end_line=5,
                content="x = 1\n",
            ),
            FileReadEvent(
                event_index=4,
                tool_name="Read",
                file_path="src/diffed.py",
                start_line=1,
                end_line=5,
                content="def run():\n    pass\n",
            ),
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "In `src/a.py:42`, it calls `return verify_token(user, secret)`. "
                    "In `src/auth.py`, it sets `timeout = 30`. "
                    "In `src/diffed.py`, it calls `return compute_result(1, 2)`."
                ),
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr).findings == []


def test_r2_8_unrelated_root_grep_and_bash_find_do_not_bypass() -> None:
    # Unrelated root Grep (`import os`) + "no callers of `legacy_auth`" -> NO_SEARCH_EVIDENCE
    tr_unrelated = ParsedTranscript(
        cwd="/repo",
        searches=[SearchEvent(event_index=0, tool_name="Grep", query="import os", search_path=".")],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="There are no callers of `legacy_auth` anywhere in the repo.",
                is_final=True,
            )
        ],
    )
    rep_unrel = verify_transcript(tr_unrelated)
    assert rep_unrel.passed is False
    assert any(f.code == "NO_SEARCH_EVIDENCE" for f in rep_unrel.findings)

    # Bash:find / Bash:fd listing-only -> NO_SEARCH_EVIDENCE
    tr_find = ParsedTranscript(
        cwd="/repo",
        searches=[
            SearchEvent(
                event_index=0,
                tool_name="Bash:find",
                query="*.py",
                search_path=".",
                raw_input={"command": "find . -name '*.py'"},
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="There are no callers of `legacy_auth` anywhere in the repo.",
                is_final=True,
            )
        ],
    )
    rep_find = verify_transcript(tr_find)
    assert rep_find.passed is False
    assert any(f.code == "NO_SEARCH_EVIDENCE" for f in rep_find.findings)


def test_r2_9_followup_bullets_coordinated_prospective_and_untouched_are_clean() -> None:
    tr = ParsedTranscript(
        cwd="/repo",
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "I left `src/b.py` untouched.\n"
                    "Next step: add `tests/test_a.py` and document it in `docs/usage.md`.\n\n"
                    "Follow-ups:\n"
                    "- Add coverage in `tests/test_a.py`\n"
                    "- Wire it into `src/cli.py` later\n"
                ),
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr).findings == []


def test_r2_10_rename_and_move_summaries_clean_with_and_without_repo(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

    # 1. git mv src/a.py src/b.py -> "Renamed `src/a.py` to `src/b.py`."
    tr_git_mv = ParsedTranscript(
        cwd=str(tmp_path),
        commands=[
            CommandEvent(
                event_index=0,
                tool_name="Bash",
                command="git mv src/a.py src/b.py",
                stdout="",
                exit_code=0,
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Renamed `src/a.py` to `src/b.py`.", is_final=True)
        ],
    )
    assert verify_transcript(tr_git_mv, repo_root=tmp_path).findings == []

    # 2. mv + delete edit -> "Moved `src/a.py` to `src/b.py`." & "helpers now live in `src/b.py`."
    tr_mv = ParsedTranscript(
        cwd=str(tmp_path),
        file_reads=[
            FileReadEvent(
                event_index=0, tool_name="Read", file_path="src/a.py", start_line=1, end_line=5
            )
        ],
        file_edits=[
            FileEditEvent(
                event_index=1, tool_name="Bash", file_path="src/a.py", operation="delete"
            ),
            FileEditEvent(
                event_index=1,
                tool_name="Bash",
                file_path="src/b.py",
                operation="write",
                content="x=1\n",
            ),
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text=(
                    "Moved `src/a.py` to `src/b.py`. "
                    "The helpers from `src/a.py` now live in `src/b.py`."
                ),
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr_mv, repo_root=tmp_path).findings == []


def test_r2_nb_tilde_subagent_lineless_nul_backtrack_ansi_and_linear_line(tmp_path: Path) -> None:
    # 1. Tilde path matching `/Users/x/.claude/settings.json`
    tr_tilde = ParsedTranscript(
        cwd="/repo",
        file_reads=[
            FileReadEvent(
                event_index=0,
                tool_name="Read",
                file_path="/Users/x/.claude/settings.json",
                start_line=1,
                end_line=5,
            )
        ],
        file_edits=[
            FileEditEvent(
                event_index=1,
                tool_name="Edit",
                file_path="/Users/x/.claude/settings.json",
                operation="edit",
                old_text="a",
                new_text="b",
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Updated `~/.claude/settings.json`.", is_final=True)
        ],
    )
    assert verify_transcript(tr_tilde).findings == []

    # 2. Subagent-relayed line-less path
    tr_sub = ParsedTranscript(
        cwd="/repo",
        events=[
            ToolEvent(
                event_index=0,
                tool_name="Task",
                raw_output="Checked src/auth/login.py and confirmed.",
                has_result=True,
            )
        ],
        assistant_messages=[
            AssistantMessage(index=0, text="Subagent reviewed `src/auth/login.py`.", is_final=True)
        ],
    )
    assert verify_transcript(tr_sub).findings == []

    # 3. Backtracking bug: "`src/a.py`, line 42 of `src/b.py`" must NOT produce src/a.py:4
    ptrs = extract_pointer_claims("Check `src/a.py`, line 42 of `src/b.py`.")
    got = {(p.file_path, p.start_line, p.end_line) for p in ptrs}
    assert ("src/b.py", 42, 42) in got
    assert ("src/a.py", 4, 4) not in got

    # 4. NUL byte in cited path must not crash resolve_in_repo
    tr_nul = ParsedTranscript(
        cwd=str(tmp_path),
        assistant_messages=[
            AssistantMessage(index=0, text="See `src/a\x00b.py:1`.", is_final=True)
        ],
    )
    verify_transcript(tr_nul, repo_root=tmp_path)

    # 5. ANSI color codes in command stdout still match clean quotes
    tr_ansi = ParsedTranscript(
        cwd="/repo",
        commands=[
            CommandEvent(
                event_index=0,
                tool_name="Bash",
                command="pytest",
                stdout="\x1b[31mAssertionError: expected 200, got 403\x1b[0m\n",
                exit_code=1,
                is_error=True,
            )
        ],
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Test failed with `AssertionError: expected 200, got 403`.",
                is_final=True,
            )
        ],
    )
    assert verify_transcript(tr_ansi).findings == []

    # 6. Linear O(n) latency on 40 KB single line of plain words
    big_line = " ".join(["word"] * 8000)
    t0 = time.perf_counter()
    assert extract_pointer_claims(big_line) == []
    assert (time.perf_counter() - t0) * 1000.0 < 100.0

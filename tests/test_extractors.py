"""Unit tests for deterministic claim extraction (pointers, quotes, absence, completeness)."""

from __future__ import annotations

from handoff_verify.extractors import (
    extract_claims,
    extract_pointer_claims,
    extract_quote_claims,
    extract_scope_claims,
    extract_scope_qualifiers,
)
from handoff_verify.models import AssistantMessage, ParsedTranscript


def test_extract_pointer_formats() -> None:
    text = (
        "Checked src/auth/login.py:42 and src/auth/tokens.py:10-25, "
        "plus src/api/routes.py:88:12 and src/db/pool.py#L15 and src/db/models.py#L20-L35. "
        "Also saw utils.py (line 7), helpers.py (lines 12-18), `config.toml`, line 3, "
        "line 9 of scripts/deploy.sh, lines 5-11 in `Makefile`, and `src/core/engine.py`."
    )
    claims = extract_pointer_claims(text)
    got = {(c.file_path, c.start_line, c.end_line) for c in claims}
    assert ("src/auth/login.py", 42, 42) in got
    assert ("src/auth/tokens.py", 10, 25) in got
    assert ("src/api/routes.py", 88, 88) in got
    assert ("src/db/pool.py", 15, 15) in got
    assert ("src/db/models.py", 20, 35) in got
    assert ("utils.py", 7, 7) in got
    assert ("helpers.py", 12, 18) in got
    assert ("config.toml", 3, 3) in got
    assert ("scripts/deploy.sh", 9, 9) in got
    assert ("Makefile", 5, 11) in got
    assert ("src/core/engine.py", None, None) in got


def test_extract_pointer_markdown_link_dedupes() -> None:
    text = "See [login.py:42](file:///repo/src/auth/login.py#L42) for the check."
    claims = extract_pointer_claims(text, base_dirs=("/repo",))
    assert len(claims) == 1
    assert claims[0].file_path == "src/auth/login.py"
    assert claims[0].start_line == 42
    assert claims[0].end_line == 42


def test_extract_pointer_avoids_false_positives() -> None:
    text = (
        "Started server at http://localhost:8080/api.py:10 and "
        "https://github.com/org/repo/blob/main/app.py#L5 "
        "at 12:34:56 (also 09:30). Bound to 127.0.0.1:3000 with python:3.11 and `Node.js`. "
        "Uses `os.path` and `self.config` and env `APP_ENV=prod`. "
        "For example, `foo.py:42` or e.g. `bar.py` or such as `baz.py:10-20`. "
        "You could create `new_file.py` or pass `--config custom.yaml`. "
        "`src/missing.py` does not exist in this repo.\n"
        "```python\n"
        "# Inside a fenced block: src/ignored.py:99\n"
        "x = 1\n"
        "```\n"
    )
    claims = extract_pointer_claims(text)
    assert claims == []


def test_trailing_example_does_not_suppress_earlier_pointer() -> None:
    text = "In `src/auth.py:12`, it validates tokens (e.g., JWTs or API keys)."
    claims = extract_pointer_claims(text)
    assert len(claims) == 1
    assert claims[0].file_path == "src/auth.py"
    assert claims[0].start_line == 12


def test_extract_inline_and_fenced_quotes() -> None:
    text = (
        "In `src/auth.py:12`, it calls `verify_token(user, secret)` before returning.\n"
        "The test output failed with `AssertionError: expected 200, got 403`.\n\n"
        "Here is the current implementation in `src/auth.py:10-14`:\n"
        "```python\n"
        "def authenticate(user, secret):\n"
        "    return verify_token(user, secret)\n"
        "```\n"
    )
    claims = extract_quote_claims(text)
    assert len(claims) == 3

    by_snippet = {c.snippet: c for c in claims}
    assert "verify_token(user, secret)" in by_snippet
    assert by_snippet["verify_token(user, secret)"].file_path == "src/auth.py"
    assert by_snippet["verify_token(user, secret)"].start_line == 12

    assert "AssertionError: expected 200, got 403" in by_snippet
    assert by_snippet["AssertionError: expected 200, got 403"].file_path is None

    fenced_key = "def authenticate(user, secret):\n    return verify_token(user, secret)"
    assert fenced_key in by_snippet
    assert by_snippet[fenced_key].file_path == "src/auth.py"
    assert by_snippet[fenced_key].start_line == 10
    assert by_snippet[fenced_key].end_line == 14


def test_extract_quotes_skips_identifiers_suggestions_commands_and_diffs() -> None:
    text = (
        "In `src/auth.py:12`, `verify_token` checks the flag `--strict`.\n"
        "Updated `src/auth.py` and ran `uv run pytest -v`.\n"
        "You could fix `src/auth.py:12` by changing it to "
        "`verify_token(user, secret, strict=True)`.\n"
        "In `src/auth.py:12`, it calls `verify_token(user, ...)`.\n\n"
        "Suggested fix for `src/auth.py`:\n"
        "```python\n"
        "def authenticate(user):\n"
        "    return True\n"
        "```\n\n"
        "Proposed patch for `src/auth.py`:\n"
        "```diff\n"
        "- return False\n"
        "+ return True\n"
        "```\n"
    )
    claims = extract_quote_claims(text)
    assert claims == []


def test_extract_absence_and_completeness_claims_and_scope_qualifiers() -> None:
    text = (
        "There are no other callers in `src/auth`.\n"
        "The symbol is not used anywhere else in the codebase.\n"
        "I reviewed all pages of issues and checked every file in the repo.\n"
        "No matches for `legacy_helper` across the repository.\n"
    )
    claims = extract_scope_claims(text)
    absences = [c for c in claims if c.kind == "absence"]
    completeness = [c for c in claims if c.kind == "completeness"]

    assert len(absences) == 3
    assert absences[0].file_path == "src/auth"
    assert absences[1].file_path is None
    # `legacy_helper` after "for" must NOT be mistaken for a directory scope qualifier
    assert absences[2].file_path is None

    assert len(completeness) == 1
    assert completeness[0].file_path is None


def test_extract_scope_claims_skips_hedging_and_test_pass_prose() -> None:
    text = (
        "Ran pytest and verified all 47 tests passed with no issues.\n"
        "No files were modified during this inspection.\n"
        "I only searched `src/auth`, so I cannot confirm there are no other callers.\n"
        "I did not check all files in the repository.\n"
    )
    claims = extract_scope_claims(text)
    assert claims == []


def test_extract_scope_qualifiers_variants() -> None:
    assert extract_scope_qualifiers("No callers in `src/auth/`.") == ["src/auth"]
    assert extract_scope_qualifiers("Checked under tests/unit/") == ["tests/unit"]
    assert extract_scope_qualifiers("No occurrences in the `billing` directory.") == ["billing"]
    assert extract_scope_qualifiers("No matches for `foo` in the codebase.") == []
    assert extract_scope_qualifiers("No TODOs in `src/app.py`.") == ["src/app.py"]


def test_extract_claims_only_reads_final_turn_messages() -> None:
    transcript = ParsedTranscript(
        cwd="/repo",
        assistant_messages=[
            AssistantMessage(
                index=0,
                text="Let me check `src/unseen_intermediate.py:99` first.",
                after_event_index=0,
                is_final=False,
            ),
            AssistantMessage(
                index=1,
                text="Checked `src/real.py:10` and confirmed the fix.",
                after_event_index=2,
                is_final=True,
            ),
        ],
        final_text="Checked `src/real.py:10` and confirmed the fix.",
    )
    claims = extract_claims(transcript)
    paths = [c.file_path for c in claims if c.kind == "pointer"]
    assert paths == ["src/real.py"]

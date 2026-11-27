import type { AccordionItemData } from "@/components/tonal/accordion";
import type { KpiMetric } from "@/components/tonal/kpi-grid";
import type { SurfaceTone } from "@/components/tonal/surface-tile";

export const REPO_URL = "https://github.com/1solomonwakhungu/handoff-verify";
export const PYPI_URL = "https://pypi.org/project/handoff-verify/";
export const HARLO_URL = "https://harlo.dev";
export const TONAL_URL = "https://tonal.harlo.dev";

export type InstallMethodId = "uvx" | "install" | "plugin" | "skill";

export interface InstallMethod {
  id: InstallMethodId;
  label: string;
  caption: string;
  command: string;
  copyText: string;
}

export const INSTALL_METHODS: InstallMethod[] = [
  {
    id: "uvx",
    label: "Try now (uvx)",
    caption: "Run against your newest Claude Code session with zero install.",
    command: "uvx handoff-verify check --latest",
    copyText: "uvx handoff-verify check --latest",
  },
  {
    id: "install",
    label: "Install (uv / pip)",
    caption: "Install globally on PATH (Python 3.11+ stdlib only, zero runtime deps).",
    command: "uv tool install handoff-verify\n# or: pip install handoff-verify",
    copyText: "uv tool install handoff-verify",
  },
  {
    id: "plugin",
    label: "Claude Plugin",
    caption: "Bundles Stop & SubagentStop hooks, standalone launcher, and SKILL.md.",
    command:
      "claude plugin marketplace add 1solomonwakhungu/handoff-verify\nclaude plugin install handoff-verify@handoff-verify",
    copyText:
      "claude plugin marketplace add 1solomonwakhungu/handoff-verify && claude plugin install handoff-verify@handoff-verify",
  },
  {
    id: "skill",
    label: "Agent Skill",
    caption: "Install via skills.sh into Claude Code, Codex, Cursor, Antigravity, or OpenCode.",
    command: "npx skills add 1solomonwakhungu/handoff-verify",
    copyText: "npx skills add 1solomonwakhungu/handoff-verify",
  },
];

export type DemoScenarioId = "clean" | "truncated" | "fabricated" | "json";

export interface DemoScenario {
  id: DemoScenarioId;
  label: string;
  shortLabel: string;
  exitCode: 0 | 1;
  formatLabel: string;
  summary: string;
  command: string;
  output: string;
  highlights: string[];
}

export const DEMO_SCENARIOS: DemoScenario[] = [
  {
    id: "clean",
    label: "Clean Pass",
    shortLabel: "Clean",
    exitCode: 0,
    formatLabel: "text",
    summary:
      "Every cited file pointer, quote, and scope claim matches observed tool output. Pass --quiet-on-pass to emit zero stdout.",
    command: "handoff-verify check evals/cases/clean_01_claude_single_read.jsonl",
    output: "PASS: 0 findings (6 claims checked, 1 file read, 1.0ms)",
    highlights: [
      "Exit code 0 — safe for CI and hooks",
      "6 claims verified deterministically in ~0.6–1.0 ms",
      "--quiet-on-pass stays completely silent at 0 findings",
    ],
  },
  {
    id: "truncated",
    label: "Truncated Grep",
    shortLabel: "Truncated Grep",
    exitCode: 1,
    formatLabel: "text",
    summary:
      "Agent claimed 'no other matches' after running Grep with head_limit=5. Scope receipt records the exact truncated search event.",
    command: "handoff-verify check evals/cases/fail_06_claude_truncated_grep.jsonl",
    output: `FAIL: 1 finding (1 error; 4 claims checked, 0 files read, 0.7ms)
  [ERROR] TRUNCATED_SEARCH_OUTPUT: Claimed \`no other matches\`, but search output was truncated: Grep#0 (result limit (5) reached).
    Claim (absence): no other matches
    Evidence: truncated_searches=[{event_index=0, tool_name=Grep, query=deprecated_v1_client, reason=result limit (5) reached}]
    Next step: Re-run the search without head_limit/truncation before claiming absence or completeness.
Scope receipt:
  Searched roots: .
  Truncated outputs (1): [0] Grep (result limit (5) reached)`,
    highlights: [
      "Exit code 1 — error-level finding detected",
      "Pinpoints exact event_index=0 and head_limit=5 reason",
      "Emits a scope receipt with searched roots and truncated outputs",
    ],
  },
  {
    id: "fabricated",
    label: "Fabricated Subagent",
    shortLabel: "Subagent",
    exitCode: 1,
    formatLabel: "markdown",
    summary:
      "Sidechain subagent cited src/security/cors.py:34 without ever reading it and altered a quoted line in src/security/csrf.py.",
    command:
      "handoff-verify check evals/cases/fail_11_claude_subagent_fabricated.jsonl --format markdown",
    output: `### handoff-verify: FAIL

**Summary:** 2 findings (2 errors) · 3 claims checked · 1 file read · 0.6ms

#### Findings

- **[ERROR] \`UNREAD_FILE\`** — Cited \`src/security/cors.py\` (line 34), but the file was never read in transcript.
  - **Claim (\`pointer\`):** \`\` \`src/security/cors.py:34\` \`\`
  - **Evidence:** file_path=src/security/cors.py; claimed_range=[34, 34]; observed_in_search=false; reason=never read in transcript
  - **Next step:** Read line 34 of src/security/cors.py
- **[ERROR] \`QUOTE_NOT_FOUND\`** — Quoted snippet \`if origin not in ALLOWED_WILDCARD_ORIGINS:\` was not found in observed content of \`src/security/csrf.py\`.
  - **Claim (\`quote\`):** \`\` \`if origin not in ALLOWED_WILDCARD_ORIGINS:\` \`\` (\`src/security/csrf.py:6-9\`)
  - **Evidence:** claimed_file=src/security/csrf.py; checked_events=[file_read#0]; snippet_preview=if origin not in ALLOWED_WILDCARD_ORIGINS:
  - **Next step:** Re-read \`src/security/csrf.py\` and verify the exact code before quoting it.`,
    highlights: [
      "Markdown output built for parent agents and PR comments",
      "Catches both UNREAD_FILE and QUOTE_NOT_FOUND in one pass",
      "Every finding includes an actionable next step",
    ],
  },
  {
    id: "json",
    label: "JSON Schema 1.0",
    shortLabel: "JSON v1.0",
    exitCode: 1,
    formatLabel: "json",
    summary:
      "Stable machine-readable schema_version '1.0' output with summary counts, extracted claims, structured findings, and scope_receipt.",
    command:
      "handoff-verify check evals/cases/fail_01_claude_unread_pointer.jsonl --format json",
    output: `{
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
      "text": "\`src/auth/tokens.py:3-6\`",
      "file_path": "src/auth/tokens.py",
      "start_line": 3,
      "end_line": 6,
      "snippet": null,
      "context": "Bearer headers are parsed in \`src/auth/tokens.py:3-6\`.",
      "message_index": 0
    },
    {
      "kind": "pointer",
      "text": "\`src/auth/session_store.py:48-55\`",
      "file_path": "src/auth/session_store.py",
      "start_line": 48,
      "end_line": 55,
      "snippet": null,
      "context": "Revoked session IDs are then checked against Redis in \`src/auth/session_store.py:48-55\` before granting access.",
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
      "message": "Cited \`src/auth/session_store.py\` (lines 48-55), but the file was never read in transcript.",
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
}`,
    highlights: [
      "Versioned schema_version: '1.0' for automation and jq pipelines",
      "Separates extracted claims, findings, and scope_receipt",
      "Includes source_format auto-detection and duration_ms",
    ],
  },
];

export interface VerificationPillar {
  id: string;
  title: string;
  badge: string;
  tone: SurfaceTone;
  chipTone: "blue" | "warm" | "mint";
  description: string;
  checks: string[];
  codes: string[];
}

export const VERIFICATION_PILLARS: VerificationPillar[] = [
  {
    id: "pointers",
    title: "File Pointers & Read Windows",
    badge: "path:line · #L10-L20",
    tone: "sky",
    chipTone: "blue",
    description:
      "Confirms every cited path:line, path:start-end, or GitHub-style #L10-L20 anchor was actually read inside observed offset/limit windows.",
    checks: [
      "Verifies transcript read coverage across Read, grep -n, rg -n, git diff, and shell reads",
      "Flags post-edit line shifts when a file was edited or deleted without re-reading",
      "Optional --repo check validates live file existence, line count bounds, and on-disk drift",
    ],
    codes: [
      "UNREAD_FILE",
      "LINE_OUT_OF_READ_WINDOW",
      "STALE_FILE_READ",
      "FILE_NOT_FOUND",
      "LINE_OUT_OF_BOUNDS",
    ],
  },
  {
    id: "quotes",
    title: "Verbatim Code & Output Quotes",
    badge: "inline · fenced blocks",
    tone: "warm",
    chipTone: "warm",
    description:
      "Checks inline and fenced code snippets against what the agent actually read, wrote, or saw in shell command output.",
    checks: [
      "Matches exact snippets as well as whitespace- and operator-normalized variants",
      "Cross-checks observed file reads, edits, searches, and command stdout",
      "Detects file misattribution when a real snippet was observed in file B but cited as file A",
    ],
    codes: ["QUOTE_NOT_FOUND", "QUOTE_MISMATCH"],
  },
  {
    id: "scope",
    title: "Absence & Completeness Scope Receipts",
    badge: "search · pagination",
    tone: "mint",
    chipTone: "mint",
    description:
      "Audits sweeping claims like 'no other callers', 'not used anywhere', or 'reviewed all pages' against recorded search and cursor events.",
    checks: [
      "Catches missing searches and truncated outputs (head_limit, | head, partial reads)",
      "Flags repo-wide claims backed only by subdirectory searches or unacknowledged excluded dirs",
      "Tracks MCP and API tool responses with unread has_more or nextCursor tokens",
    ],
    codes: [
      "NO_SEARCH_EVIDENCE",
      "TRUNCATED_SEARCH_OUTPUT",
      "NARROW_SEARCH_SCOPE",
      "UNREAD_PAGINATION_CURSOR",
      "EXCLUDED_SEARCH_DIRS",
    ],
  },
];

export type FindingSeverity = "error" | "warning" | "error / warning";

export interface FindingCodeEntry {
  code: string;
  severity: FindingSeverity;
  pillar: "File Pointers" | "Verbatim Quotes" | "Scope Receipts";
  meaning: string;
}

export const FINDING_CODES: FindingCodeEntry[] = [
  {
    code: "UNREAD_FILE",
    severity: "error",
    pillar: "File Pointers",
    meaning: "Cited file or path:line was never read/observed in the transcript.",
  },
  {
    code: "LINE_OUT_OF_READ_WINDOW",
    severity: "error",
    pillar: "File Pointers",
    meaning:
      "Cited line range falls outside the transcript's observed Read line windows.",
  },
  {
    code: "STALE_FILE_READ",
    severity: "error / warning",
    pillar: "File Pointers",
    meaning:
      "File was edited or deleted after the last read (or changed on disk with --repo) without re-reading.",
  },
  {
    code: "FILE_NOT_FOUND",
    severity: "error",
    pillar: "File Pointers",
    meaning: "Cited file does not exist on disk in --repo.",
  },
  {
    code: "LINE_OUT_OF_BOUNDS",
    severity: "error",
    pillar: "File Pointers",
    meaning: "Cited line number exceeds the live file's line count in --repo.",
  },
  {
    code: "QUOTE_NOT_FOUND",
    severity: "error",
    pillar: "Verbatim Quotes",
    meaning:
      "Quoted code or command output snippet does not appear in observed evidence.",
  },
  {
    code: "QUOTE_MISMATCH",
    severity: "error",
    pillar: "Verbatim Quotes",
    meaning:
      "Quoted snippet was attributed to file A, but was only observed in file B.",
  },
  {
    code: "NO_SEARCH_EVIDENCE",
    severity: "error",
    pillar: "Scope Receipts",
    meaning:
      "Absence/completeness claim made with no matching repository search.",
  },
  {
    code: "TRUNCATED_SEARCH_OUTPUT",
    severity: "error",
    pillar: "Scope Receipts",
    meaning:
      "Absence/completeness claim backed only by truncated search or partial read output.",
  },
  {
    code: "NARROW_SEARCH_SCOPE",
    severity: "error",
    pillar: "Scope Receipts",
    meaning:
      "Repo-wide (or wider) absence claim backed only by a subdirectory search.",
  },
  {
    code: "UNREAD_PAGINATION_CURSOR",
    severity: "error",
    pillar: "Scope Receipts",
    meaning:
      "Completeness/absence claim made while a tool returned has_more / nextCursor that was never followed.",
  },
  {
    code: "EXCLUDED_SEARCH_DIRS",
    severity: "warning",
    pillar: "Scope Receipts",
    meaning:
      "Search explicitly excluded non-trivial directories not acknowledged in the claim.",
  },
];

export const CLAUDE_SETTINGS_SNIPPET = `{
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
}`;

export interface HookGuarantee {
  title: string;
  detail: string;
}

export const HOOK_GUARANTEES: HookGuarantee[] = [
  {
    title: "Strict SubagentStop isolation",
    detail:
      "Reads only agent_transcript_path on SubagentStop and never falls back to the parent session's transcript_path.",
  },
  {
    title: "last_assistant_message race handling",
    detail:
      "Waits up to 1s for the transcript flush (skipping re-parses when size and mtime are unchanged), then falls back to verifying the payload text against on-disk tool evidence.",
  },
  {
    title: "Silent on clean & warning-only passes",
    detail:
      "Exits 0 with empty stdout and stderr when clean or warning-only (unless --include-warnings or --strict is passed).",
  },
  {
    title: "advise (default) vs block modes",
    detail:
      "advise exits 0 with structured systemMessage + hookSpecificOutput.additionalContext; block writes concise feedback to stderr and exits 2.",
  },
  {
    title: "stop_hook_active infinite-loop guard",
    detail:
      "When Claude Code sets stop_hook_active: true, never exits 2 and never emits additionalContext — capping retries at one.",
  },
  {
    title: "Fail-open with visible diagnostics",
    detail:
      "Parse, I/O, or internal errors log one line to stderr and exit 1 (non-blocking) instead of bricking a session; pass --fail-closed to exit 2.",
  },
];

export interface TranscriptFormatInfo {
  id: string;
  name: string;
  parserFlag: string;
  tone: SurfaceTone;
  pathHint: string;
  description: string;
}

export const TRANSCRIPT_FORMATS: TranscriptFormatInfo[] = [
  {
    id: "claude_code",
    name: "Claude Code JSONL",
    parserFlag: "--parser claude_code",
    tone: "cool",
    pathHint: "~/.claude/projects/<cwd>/<session-id>.jsonl",
    description:
      "Main session and sidechain subagent logs: Read, Grep, Glob, LS, Bash, Edit, MultiEdit, Write, NotebookEdit, WebFetch, and mcp__* paginated tools. Auto-discovered via --latest.",
  },
  {
    id: "codex",
    name: "Codex CLI JSONL",
    parserFlag: "--parser codex",
    tone: "warm",
    pathHint: "~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl",
    description:
      "Modern rollout logs (session_meta, turn_context, response_item with function_call / function_call_output, custom_tool_call / apply_patch, local_shell_call, event_msg) plus legacy flat logs.",
  },
  {
    id: "generic",
    name: "Generic JSONL",
    parserFlag: "--parser generic",
    tone: "neutral",
    pathHint:
      "meta · file_read · file_edit · search · command · pagination · tool_call / tool_result · assistant",
    description:
      "Documented line-delimited JSON schema for integrating any custom agent harness or orchestration pipeline with auto-detection.",
  },
];

export const BENCHMARK_KPIS: KpiMetric[] = [
  {
    id: "precision",
    title: "Seeded Precision",
    value: "100.0%",
    delta: "16 TP · 0 FP",
    tone: "mint",
    subtitle: "Target 100.0% · 32-case suite",
  },
  {
    id: "recall",
    title: "Seeded Recall",
    value: "100.0%",
    delta: "16 / 16",
    tone: "mint",
    subtitle: "Target ≥ 90.0% · 100% code match",
  },
  {
    id: "fpr",
    title: "False Positive Rate",
    value: "0.0%",
    delta: "0 / 16 clean",
    tone: "mint",
    subtitle: "Target 0.0% on seeded clean cases",
  },
  {
    id: "latency",
    title: "Median Latency",
    value: "0.60 ms",
    delta: "P95 1.38 ms",
    tone: "blue",
    subtitle: "Target < 50.0 ms · max 1.56 ms",
  },
];

export interface ScalingRow {
  workload: string;
  size: string;
  parse: string;
  verify: string;
  total: string;
  throughput: string;
}

export const SCALING_ROWS: ScalingRow[] = [
  {
    workload: "Small session (25 reads × 100 lines)",
    size: "115 KB",
    parse: "1.28 ms",
    verify: "1.22 ms",
    total: "2.50 ms",
    throughput: "44.8 MB/s",
  },
  {
    workload: "Medium session (250 reads × 100 lines)",
    size: "1.14 MB",
    parse: "9.98 ms",
    verify: "2.73 ms",
    total: "12.74 ms",
    throughput: "89.4 MB/s",
  },
  {
    workload: "Large session (1,250 reads × 100 lines)",
    size: "5.76 MB",
    parse: "48.53 ms",
    verify: "10.03 ms",
    total: "60.77 ms",
    throughput: "94.7 MB/s",
  },
  {
    workload: "End-to-end CLI (clean_01 --quiet-on-pass, 15 runs)",
    size: "1.6 KB",
    parse: "—",
    verify: "—",
    total: "56.8 ms",
    throughput: "~54.7 ms Python startup",
  },
];

export const REAL_SESSION_FIXES: string[] = [
  "Shell output redirected to a temp/log file (> /tmp/x.log, | tee ...) and read back",
  "grep -n / rg -n context lines (-C / -A / -B file-line-content) and adjacent lines",
  "Parent agents relaying Agent/Task subagent path:line ranges under alternate spellings or unambiguous basenames",
  "Negated existence mentions (e.g., \"src/app.py:99 does not exist\", \"there is no foo.py:42\")",
  "Non-code absence phrases (\"no usage limit\", \"no open issue\", \"no open PRs\", \"no errors\", \"no warnings\", \"no changes\")",
];

export const FAQ_ITEMS: AccordionItemData[] = [
  {
    question: "Why deterministic verification instead of an LLM judge?",
    answer:
      "All of the ground truth — every file read window, edit, grep output, shell command, and pagination cursor — is already stored locally in the agent's JSONL transcript. Checking claims deterministically in pure Python stdlib runs in sub-millisecond time (0.60 ms median on the 32-case suite), incurs zero token cost, works offline, and produces reproducible findings with exact evidence pointers.",
  },
  {
    question: "Does handoff-verify check whether the code or tests are correct?",
    answer:
      "No. handoff-verify checks grounding against the transcript — whether cited files, line windows, verbatim quotes, and search/pagination completeness claims are actually backed by observed tool outputs. It does not judge code quality, architecture, or test correctness. Treat any finding as 'the transcript does not show this', not 'this is false'.",
  },
  {
    question: "Should I start with advise mode or block mode in Claude Code hooks?",
    answer:
      "Start with the default --mode advise (installed by handoff-verify init --write). On error-level findings, advise mode exits 0 and emits structured JSON (systemMessage + hookSpecificOutput.additionalContext), which continues the turn once so the agent can correct its report while remaining loop-guarded by stop_hook_active. Once you have observed how it behaves on your workflows — especially if your agents read files via custom inline scripts — you can opt into --mode block (exit 2 on findings).",
  },
  {
    question: "Can a Stop or SubagentStop hook cause an infinite retry loop?",
    answer:
      "No. When Claude Code triggers a second stop pass and sets stop_hook_active: true in the hook payload, handoff-verify never exits 2 and never emits hookSpecificOutput.additionalContext. It emits systemMessage only and exits 0, guaranteeing the agent can always stop after at most one verification retry.",
  },
  {
    question: "How does it work with Codex CLI or custom agent harnesses?",
    answer:
      "handoff-verify auto-detects Claude Code JSONL, Codex CLI rollout JSONL (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl), and a documented Generic JSONL schema (meta, file_read, file_edit, search, command, pagination, tool_call/tool_result, assistant). Note that --latest currently discovers Claude Code session directories; for Codex CLI or custom transcripts, pass the file path (or '-' for stdin) explicitly.",
  },
  {
    question: "What exit codes does the CLI return?",
    answer:
      "For check: exit 0 means verification passed (no error-level findings; pass --strict to fail on warnings too), exit 1 means one or more findings were detected, and exit 2 means a usage error, unreadable transcript, or internal error. For eval: exit 0 when all targets pass, exit 1 if targets are missed, and exit 2 on a missing or invalid manifest. For hook claude-code: exit 0 when clean or in advise mode, exit 2 on findings in block mode, and exit 1 (fail-open with one stderr diagnostic) on parse/I/O errors unless --fail-closed is set.",
  },
];

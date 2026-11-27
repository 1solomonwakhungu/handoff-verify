"use client";

import * as React from "react";
import {
  AlertTriangle,
  ArrowUpRight,
  Check,
  CheckCircle2,
  FileCode2,
  GitBranch,
  Moon,
  Quote,
  Radar,
  Search,
  ShieldCheck,
  Sun,
  Terminal,
  X,
} from "lucide-react";
import { SurfaceTile } from "@/components/tonal/surface-tile";
import { TonalChip } from "@/components/tonal/chip";
import { TonalButton } from "@/components/tonal/button";
import { ChevronLink } from "@/components/tonal/chevron-link";
import { TonalAccordion } from "@/components/tonal/accordion";
import { TonalSegmentedControl } from "@/components/tonal/segmented-control";
import { KpiGrid } from "@/components/tonal/kpi-grid";
import { CodeBlock } from "@/components/code-block";
import {
  BENCHMARK_KPIS,
  CLAUDE_SETTINGS_SNIPPET,
  DEMO_SCENARIOS,
  FAQ_ITEMS,
  FINDING_CODES,
  HARLO_URL,
  HOOK_GUARANTEES,
  INSTALL_METHODS,
  PYPI_URL,
  REAL_SESSION_FIXES,
  REPO_URL,
  SCALING_ROWS,
  TONAL_URL,
  TRANSCRIPT_FORMATS,
  VERIFICATION_PILLARS,
  type DemoScenarioId,
  type FindingSeverity,
  type InstallMethodId,
} from "@/lib/content";

type SeverityFilter = "all" | "error" | "warning";

const NAV_LINKS = [
  { href: "#checks", label: "Checks" },
  { href: "#demo", label: "Demo" },
  { href: "#hooks", label: "Hooks" },
  { href: "#benchmarks", label: "Benchmarks" },
  { href: "#faq", label: "FAQ" },
] as const;

const PILLAR_ICONS = {
  pointers: FileCode2,
  quotes: Quote,
  scope: Radar,
} as const;

function BrandMark({ className = "h-6 w-6" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      className={className}
    >
      <rect width="32" height="32" rx="10" fill="var(--t-ink)" />
      <rect
        x="7.5"
        y="6.5"
        width="13"
        height="17"
        rx="3"
        stroke="var(--t-bg)"
        strokeWidth="2"
      />
      <path
        d="M11 11.5h6M11 15.5h4"
        stroke="var(--t-blue)"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <circle cx="21.5" cy="21.5" r="5.5" fill="var(--t-ink)" />
      <circle cx="21.5" cy="21.5" r="4.5" fill="var(--t-success)" />
      <path
        d="M19.3 21.5l1.5 1.5 3-3"
        stroke="var(--t-bg)"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function SeverityChip({ severity }: { severity: FindingSeverity }) {
  if (severity === "error") {
    return (
      <TonalChip tone="danger" className="font-mono text-[11px] px-2.5 py-0.5">
        error
      </TonalChip>
    );
  }
  if (severity === "warning") {
    return (
      <TonalChip tone="warning" className="font-mono text-[11px] px-2.5 py-0.5">
        warning
      </TonalChip>
    );
  }
  return (
    <span className="inline-flex items-center gap-1">
      <TonalChip tone="danger" className="font-mono text-[11px] px-2 py-0.5">
        error
      </TonalChip>
      <TonalChip tone="warning" className="font-mono text-[11px] px-2 py-0.5">
        warning
      </TonalChip>
    </span>
  );
}

export function LandingPage() {
  const [theme, setTheme] = React.useState<"light" | "dark">("light");
  const [installMethod, setInstallMethod] =
    React.useState<InstallMethodId>("uvx");
  const [demoScenario, setDemoScenario] =
    React.useState<DemoScenarioId>("truncated");
  const [severityFilter, setSeverityFilter] =
    React.useState<SeverityFilter>("all");
  const [codeQuery, setCodeQuery] = React.useState("");

  React.useEffect(() => {
    try {
      const ds = document.documentElement.dataset;
      if (ds.tonalTheme === "dark" || ds.tonalTheme === "light") {
        setTheme(ds.tonalTheme);
      } else if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
        document.documentElement.dataset.tonalTheme = "dark";
        setTheme("dark");
      }
    } catch {
      // Ignore storage access errors
    }
  }, []);

  const toggleTheme = React.useCallback(() => {
    setTheme((prev) => {
      const next = prev === "dark" ? "light" : "dark";
      document.documentElement.dataset.tonalTheme = next;
      try {
        window.localStorage.setItem("handoff-verify-theme", next);
      } catch {
        // Ignore storage errors
      }
      return next;
    });
  }, []);

  const activeInstall =
    INSTALL_METHODS.find((m) => m.id === installMethod) ?? INSTALL_METHODS[0];
  const activeDemo =
    DEMO_SCENARIOS.find((d) => d.id === demoScenario) ?? DEMO_SCENARIOS[0];

  const errorCount = FINDING_CODES.filter((c) =>
    c.severity.includes("error")
  ).length;
  const warningCount = FINDING_CODES.filter((c) =>
    c.severity.includes("warning")
  ).length;

  const filteredCodes = React.useMemo(() => {
    const q = codeQuery.trim().toLowerCase();
    return FINDING_CODES.filter((entry) => {
      const matchesSeverity =
        severityFilter === "all" ||
        (severityFilter === "error" && entry.severity.includes("error")) ||
        (severityFilter === "warning" && entry.severity.includes("warning"));
      if (!matchesSeverity) return false;
      if (!q) return true;
      return (
        entry.code.toLowerCase().includes(q) ||
        entry.meaning.toLowerCase().includes(q) ||
        entry.pillar.toLowerCase().includes(q)
      );
    });
  }, [codeQuery, severityFilter]);

  return (
    <div className="min-h-screen bg-[var(--t-bg)] text-[var(--t-ink)] flex flex-col transition-colors duration-200">
      {/* Sticky Header */}
      <header className="sticky top-0 z-40 bg-[var(--t-bg)]/90 backdrop-blur-md border-b border-[var(--t-border-subtle)] transition-colors duration-200">
        <div className="mx-auto max-w-6xl px-5 sm:px-8 h-16 flex items-center justify-between gap-4">
          <a
            href="#"
            className="flex items-center gap-2.5 text-[16px] font-medium tracking-[-0.02em] text-[var(--t-ink)] shrink-0 rounded-[8px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
          >
            <BrandMark className="h-6 w-6 shrink-0" />
            <span>handoff-verify</span>
          </a>

          <nav
            aria-label="Primary navigation"
            className="hidden lg:flex items-center gap-1"
          >
            {NAV_LINKS.map((link) => (
              <a
                key={link.href}
                href={link.href}
                className="rounded-full px-3.5 py-1.5 text-[13.5px] font-medium text-[var(--t-ink-secondary)] hover:text-[var(--t-ink)] hover:bg-[var(--t-surface)] transition-colors whitespace-nowrap focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
              >
                {link.label}
              </a>
            ))}
          </nav>

          <div className="flex items-center gap-2 shrink-0">
            <a
              href={HARLO_URL}
              className="hidden sm:inline-flex items-center gap-1 rounded-full px-3 py-1.5 text-[12.5px] font-medium text-[var(--t-ink-muted)] hover:text-[var(--t-ink)] hover:bg-[var(--t-surface)] transition-colors whitespace-nowrap focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              <span>harlo.dev</span>
              <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
            </a>

            <a
              href={REPO_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-full bg-[var(--t-surface)] hover:bg-[var(--t-surface-muted)] border border-[var(--t-border-subtle)] px-3.5 h-8 sm:h-9 text-[12.5px] font-medium text-[var(--t-ink)] transition-colors whitespace-nowrap focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              <GitBranch className="h-3.5 w-3.5" aria-hidden="true" />
              <span>GitHub</span>
            </a>

            <button
              type="button"
              onClick={toggleTheme}
              aria-label={
                theme === "dark"
                  ? "Switch to light mode"
                  : "Switch to dark mode"
              }
              title={
                theme === "dark"
                  ? "Switch to light mode"
                  : "Switch to dark mode"
              }
              className="inline-flex h-8 w-8 sm:h-9 sm:w-9 shrink-0 items-center justify-center rounded-full bg-[var(--t-surface)] border border-[var(--t-border-subtle)] text-[var(--t-ink-secondary)] hover:text-[var(--t-ink)] hover:bg-[var(--t-surface-muted)] cursor-pointer transition-colors duration-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              {theme === "dark" ? (
                <Sun className="h-4 w-4" aria-hidden="true" />
              ) : (
                <Moon className="h-4 w-4" aria-hidden="true" />
              )}
            </button>
          </div>
        </div>
      </header>

      <main className="flex-1 mx-auto w-full max-w-6xl px-5 sm:px-8 pt-10 sm:pt-16 pb-20 flex flex-col gap-20 sm:gap-24">
        {/* Hero Section */}
        <section className="grid grid-cols-1 lg:grid-cols-12 gap-8 lg:gap-10 items-start">
          <div className="lg:col-span-7 flex flex-col items-start gap-6">
            <TonalChip tone="blue">
              <span
                aria-hidden="true"
                className="h-1.5 w-1.5 rounded-full bg-[var(--t-success)]"
              />
              v0.1.1 · Zero-LLM · Python 3.11+ stdlib
            </TonalChip>

            <div className="flex flex-col gap-4">
              <h1 className="t-display text-[36px] sm:text-[50px] lg:text-[54px]">
                Verify AI agent handoffs against their transcripts.
              </h1>
              <p className="t-body text-[16px] sm:text-[17.5px] text-[var(--t-ink-secondary)] max-w-2xl">
                AI coding agents and subagents routinely cite{" "}
                <code className="font-mono text-[0.9em] rounded px-1.5 py-0.5 bg-[var(--t-surface-muted)] text-[var(--t-ink)]">
                  path:line
                </code>{" "}
                ranges they never read, alter quoted snippets, or claim{" "}
                <em>&ldquo;no other callers&rdquo;</em> after a truncated grep.{" "}
                <strong className="font-medium text-[var(--t-ink)]">
                  handoff-verify
                </strong>{" "}
                checks every claim deterministically against the local JSONL
                tool-call transcript — zero LLM calls, zero third-party runtime
                dependencies.
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <a
                href="#demo"
                className="inline-flex items-center justify-center gap-2 font-medium leading-none tracking-[-0.01em] rounded-[var(--t-radius-btn)] px-6 py-3 text-[14px] bg-[var(--t-cta-bg)] text-[var(--t-cta-fg,#ffffff)] hover:bg-[var(--t-cta-hover)] transition-all duration-200 [transition-timing-function:var(--t-ease)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
              >
                Get started
              </a>
              <a
                href={REPO_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center justify-center gap-2 font-medium leading-none tracking-[-0.01em] rounded-[var(--t-radius-btn)] px-6 py-3 text-[14px] bg-transparent text-[var(--t-ink)] border border-[var(--t-ink-muted)] hover:bg-[var(--t-hover-overlay)] transition-all duration-200 [transition-timing-function:var(--t-ease)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
              >
                <span>View on GitHub</span>
                <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
              </a>
            </div>

            {/* Install Switcher */}
            <div className="w-full flex flex-col gap-3 pt-2">
              <TonalSegmentedControl<InstallMethodId>
                ariaLabel="Choose installation method"
                size="sm"
                className="self-start"
                options={INSTALL_METHODS.map((m) => ({
                  value: m.id,
                  label: m.label,
                }))}
                value={installMethod}
                onValueChange={setInstallMethod}
              />
              <CodeBlock
                label={activeInstall.label}
                code={activeInstall.command}
                copyText={activeInstall.copyText}
              />
              <p className="text-[13px] text-[var(--t-ink-muted)]">
                {activeInstall.caption}
              </p>
            </div>
          </div>

          {/* Live Terminal Preview Card */}
          <div className="lg:col-span-5 w-full">
            <SurfaceTile
              tone="cool"
              bordered
              padding="md"
              className="flex flex-col gap-4"
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Terminal
                    className="h-4 w-4 text-[var(--t-blue)]"
                    aria-hidden="true"
                  />
                  <span className="text-[13px] font-medium text-[var(--t-ink)]">
                    Local transcript check
                  </span>
                </div>
                <TonalChip tone="mint" className="text-[11px] px-2.5 py-0.5">
                  0.60 ms median
                </TonalChip>
              </div>

              <div className="rounded-[18px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-4 font-mono text-[12px] leading-[1.65] overflow-x-auto whitespace-pre-wrap">
                <div className="text-[var(--t-ink-muted)]">
                  $ handoff-verify check
                  evals/cases/fail_06_claude_truncated_grep.jsonl
                </div>
                <div className="mt-2 font-medium text-[var(--t-danger)]">
                  FAIL: 1 finding (1 error; 4 claims checked, 0 files read,
                  0.7ms)
                </div>
                <div className="mt-1.5 text-[var(--t-ink)]">
                  {"  "}
                  <span className="text-[var(--t-danger)]">[ERROR]</span>{" "}
                  TRUNCATED_SEARCH_OUTPUT: Claimed `no other matches`, but
                  search output was truncated: Grep#0 (result limit (5)
                  reached).
                </div>
                <div className="mt-1 text-[var(--t-ink-secondary)]">
                  {"    "}Claim (absence): no other matches
                </div>
                <div className="mt-1 text-[var(--t-blue)]">
                  {"    "}Next step: Re-run the search without
                  head_limit/truncation before claiming absence or completeness.
                </div>
                <div className="mt-2.5 pt-2.5 border-t border-[var(--t-border-subtle)] text-[var(--t-ink-muted)]">
                  Scope receipt:
                  <div className="text-[var(--t-ink-secondary)]">
                    {"  "}Searched roots: .
                  </div>
                  <div className="text-[var(--t-ink-secondary)]">
                    {"  "}Truncated outputs (1): [0] Grep (result limit (5)
                    reached)
                  </div>
                </div>
              </div>

              <div className="rounded-[16px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] px-4 py-3 font-mono text-[12px] flex items-center justify-between gap-3">
                <span className="text-[var(--t-success)] truncate">
                  PASS: 0 findings (6 claims checked, 1 file read, 1.0ms)
                </span>
                <TonalChip
                  tone="mint"
                  className="text-[11px] px-2 py-0.5 shrink-0"
                >
                  exit 0
                </TonalChip>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-2 pt-1 text-[12.5px] text-[var(--t-ink-muted)]">
                <span>Silent on clean passes with --quiet-on-pass</span>
                <ChevronLink href="#demo" className="text-[13px]">
                  Explore all 4 outputs
                </ChevronLink>
              </div>
            </SurfaceTile>
          </div>
        </section>

        {/* Interactive Transcript Verification Explorer */}
        <section id="demo" className="flex flex-col gap-6">
          <div className="flex flex-col lg:flex-row lg:items-end justify-between gap-4">
            <div className="max-w-2xl">
              <TonalChip tone="neutral" className="mb-3">
                Interactive Transcript Explorer
              </TonalChip>
              <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
                Real CLI outputs from the bundled eval suite
              </h2>
              <p className="t-body text-[15px] text-[var(--t-ink-muted)]">
                Inspect exact plain-text, Markdown, and machine-readable JSON (
                <code className="font-mono text-[0.9em]">schema_version: &quot;1.0&quot;</code>
                ) receipts returned by{" "}
                <code className="font-mono text-[0.9em]">handoff-verify check</code>.
              </p>
            </div>

            <TonalSegmentedControl<DemoScenarioId>
              ariaLabel="Select verification scenario"
              size="sm"
              className="self-start lg:self-end shrink-0"
              options={DEMO_SCENARIOS.map((s) => ({
                value: s.id,
                label: s.shortLabel,
              }))}
              value={demoScenario}
              onValueChange={setDemoScenario}
            />
          </div>

          <SurfaceTile
            tone="neutral"
            bordered
            padding="md"
            className="flex flex-col gap-5"
          >
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="t-headline text-[20px]">{activeDemo.label}</h3>
                <TonalChip
                  tone={activeDemo.exitCode === 0 ? "mint" : "danger"}
                  className="font-mono text-[11px]"
                >
                  exit {activeDemo.exitCode}
                </TonalChip>
                <TonalChip tone="neutral" className="font-mono text-[11px]">
                  --format {activeDemo.formatLabel}
                </TonalChip>
              </div>
            </div>

            <p className="t-body text-[14.5px] max-w-3xl">
              {activeDemo.summary}
            </p>

            <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
              <div className="lg:col-span-8 flex flex-col gap-3 min-w-0">
                <CodeBlock
                  label="Command"
                  code={`$ ${activeDemo.command}`}
                  copyText={activeDemo.command}
                />
                <CodeBlock
                  label={`Output (${activeDemo.formatLabel})`}
                  code={activeDemo.output}
                  preClassName="max-h-[420px]"
                />
              </div>

              <div className="lg:col-span-4 flex flex-col gap-3">
                <div className="rounded-[18px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-5 flex flex-col gap-3.5 h-full">
                  <span className="text-[12.5px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                    What this demonstrates
                  </span>
                  <ul className="flex flex-col gap-3">
                    {activeDemo.highlights.map((item) => (
                      <li
                        key={item}
                        className="flex items-start gap-2.5 text-[13.5px] leading-[1.5] text-[var(--t-ink-secondary)]"
                      >
                        <CheckCircle2
                          className="h-4 w-4 shrink-0 mt-0.5 text-[var(--t-blue)]"
                          aria-hidden="true"
                        />
                        <span>{item}</span>
                      </li>
                    ))}
                  </ul>
                  <div className="mt-auto pt-3 border-t border-[var(--t-border-subtle)] text-[12.5px] text-[var(--t-ink-muted)]">
                    Exit codes: <code className="font-mono">0</code> pass ·{" "}
                    <code className="font-mono">1</code> findings ·{" "}
                    <code className="font-mono">2</code> usage/parse error
                  </div>
                </div>
              </div>
            </div>
          </SurfaceTile>
        </section>

        {/* 3 Verification Pillars Bento */}
        <section id="checks" className="flex flex-col gap-6">
          <div className="max-w-2xl">
            <TonalChip tone="blue" className="mb-3">
              Three Deterministic Checks
            </TonalChip>
            <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
              Every claim cross-checked against observed tool evidence
            </h2>
            <p className="t-body text-[15px] text-[var(--t-ink-muted)]">
              Instead of grading prose with another model, handoff-verify
              extracts structured claims from the final turn and reconciles them
              against file reads, edits, searches, command outputs, and
              pagination cursors.
            </p>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
            {VERIFICATION_PILLARS.map((pillar) => {
              const Icon =
                PILLAR_ICONS[pillar.id as keyof typeof PILLAR_ICONS] ??
                FileCode2;
              return (
                <SurfaceTile
                  key={pillar.id}
                  tone={pillar.tone}
                  bordered
                  padding="md"
                  className="flex flex-col justify-between gap-6"
                >
                  <div className="flex flex-col gap-4">
                    <div className="flex items-center justify-between gap-3">
                      <div className="inline-flex h-11 w-11 items-center justify-center rounded-[var(--t-radius-sm)] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] text-[var(--t-ink)]">
                        <Icon className="h-5 w-5" aria-hidden="true" />
                      </div>
                      <TonalChip
                        tone={pillar.chipTone}
                        className="font-mono text-[11px]"
                      >
                        {pillar.badge}
                      </TonalChip>
                    </div>

                    <div>
                      <h3 className="t-headline text-[21px] mb-2">
                        {pillar.title}
                      </h3>
                      <p className="t-body text-[14.5px]">
                        {pillar.description}
                      </p>
                    </div>

                    <ul className="flex flex-col gap-2.5 pt-1">
                      {pillar.checks.map((check) => (
                        <li
                          key={check}
                          className="flex items-start gap-2 text-[13.5px] leading-[1.5] text-[var(--t-ink-secondary)]"
                        >
                          <Check
                            className="h-4 w-4 shrink-0 mt-0.5 text-[var(--t-ink)]"
                            aria-hidden="true"
                          />
                          <span>{check}</span>
                        </li>
                      ))}
                    </ul>
                  </div>

                  <div className="pt-4 border-t border-[var(--t-border-subtle)]/80 flex flex-wrap gap-1.5">
                    {pillar.codes.map((code) => (
                      <code
                        key={code}
                        className="rounded-full bg-[var(--t-elevated)]/90 border border-[var(--t-border-subtle)] px-2.5 py-0.5 text-[11px] font-mono text-[var(--t-ink-secondary)]"
                      >
                        {code}
                      </code>
                    ))}
                  </div>
                </SurfaceTile>
              );
            })}
          </div>
        </section>

        {/* All 12 Finding Codes Reference */}
        <section id="codes" className="flex flex-col gap-6">
          <div className="flex flex-col xl:flex-row xl:items-end justify-between gap-4">
            <div className="max-w-2xl">
              <TonalChip tone="neutral" className="mb-3">
                Stable Finding Codes
              </TonalChip>
              <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
                All 12 deterministic finding codes
              </h2>
              <p className="t-body text-[15px] text-[var(--t-ink-muted)]">
                Every finding emits a stable code, severity, extracted claim,
                transcript evidence, and next-step suggestion. Pass{" "}
                <code className="font-mono text-[0.9em]">--strict</code> to
                treat warnings as failures.
              </p>
            </div>

            <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 shrink-0">
              <div className="relative">
                <Search
                  aria-hidden="true"
                  className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-[var(--t-ink-muted)]"
                />
                <input
                  type="search"
                  value={codeQuery}
                  onChange={(e) => setCodeQuery(e.target.value)}
                  placeholder="Filter codes..."
                  aria-label="Filter finding codes"
                  className="w-full sm:w-52 rounded-full bg-[var(--t-surface)] border border-[var(--t-border-subtle)] pl-9 pr-8 py-1.5 text-[13px] text-[var(--t-ink)] placeholder:text-[var(--t-ink-muted)] focus:bg-[var(--t-elevated)] focus:border-[var(--t-ring)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
                />
                {codeQuery && (
                  <button
                    type="button"
                    onClick={() => setCodeQuery("")}
                    aria-label="Clear code filter"
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 inline-flex h-5 w-5 items-center justify-center rounded-full text-[var(--t-ink-muted)] hover:text-[var(--t-ink)] cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
                  >
                    <X className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                )}
              </div>

              <TonalSegmentedControl<SeverityFilter>
                ariaLabel="Filter finding codes by severity"
                size="sm"
                className="self-start sm:self-auto shrink-0"
                options={[
                  { value: "all", label: "All", badge: FINDING_CODES.length },
                  { value: "error", label: "Errors", badge: errorCount },
                  { value: "warning", label: "Warnings", badge: warningCount },
                ]}
                value={severityFilter}
                onValueChange={setSeverityFilter}
              />
            </div>
          </div>

          {filteredCodes.length === 0 ? (
            <SurfaceTile
              tone="neutral"
              bordered
              padding="md"
              className="text-center flex flex-col items-center gap-3"
            >
              <p className="t-body text-[14.5px] text-[var(--t-ink-muted)]">
                No finding codes match &ldquo;{codeQuery}&rdquo;.
              </p>
              <TonalButton
                variant="tonal"
                size="sm"
                onClick={() => {
                  setCodeQuery("");
                  setSeverityFilter("all");
                }}
              >
                Reset filter
              </TonalButton>
            </SurfaceTile>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {filteredCodes.map((item) => (
                <SurfaceTile
                  key={item.code}
                  tone="white"
                  bordered
                  padding="sm"
                  className="flex flex-col justify-between gap-3"
                >
                  <div className="flex flex-col gap-2.5">
                    <div className="flex items-start justify-between gap-2">
                      <code className="font-mono text-[13px] font-medium text-[var(--t-ink)] break-all">
                        {item.code}
                      </code>
                      <div className="shrink-0">
                        <SeverityChip severity={item.severity} />
                      </div>
                    </div>
                    <p className="text-[13.5px] leading-[1.55] text-[var(--t-ink-secondary)]">
                      {item.meaning}
                    </p>
                  </div>
                  <div className="pt-2 border-t border-[var(--t-border-subtle)] text-[11.5px] font-medium text-[var(--t-ink-muted)]">
                    {item.pillar}
                  </div>
                </SurfaceTile>
              ))}
            </div>
          )}
        </section>

        {/* Claude Code Hooks & Multi-Harness Support */}
        <section id="hooks" className="flex flex-col gap-6">
          <div className="max-w-2xl">
            <TonalChip tone="mint" className="mb-3">
              Hooks & Multi-Harness Support
            </TonalChip>
            <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
              Automatic Stop & SubagentStop hooks for Claude Code
            </h2>
            <p className="t-body text-[15px] text-[var(--t-ink-muted)]">
              Run{" "}
              <code className="font-mono text-[0.9em]">
                handoff-verify init --write
              </code>{" "}
              to merge hooks safely into{" "}
              <code className="font-mono text-[0.9em]">
                .claude/settings.json
              </code>{" "}
              (or pass{" "}
              <code className="font-mono text-[0.9em]">
                --settings ~/.claude/settings.json
              </code>{" "}
              for every project), then run{" "}
              <code className="font-mono text-[0.9em]">
                handoff-verify doctor
              </code>{" "}
              to verify PATH, Python 3.11+, and hook config.
            </p>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-12 gap-5 items-stretch">
            <SurfaceTile
              tone="cool"
              bordered
              padding="md"
              className="lg:col-span-6 flex flex-col gap-4"
            >
              <div className="flex items-center justify-between gap-2">
                <h3 className="t-headline text-[20px]">
                  Generated .claude/settings.json
                </h3>
                <TonalChip tone="blue" className="font-mono text-[11px]">
                  init --write
                </TonalChip>
              </div>

              <CodeBlock
                label=".claude/settings.json"
                code={CLAUDE_SETTINGS_SNIPPET}
              />

              <CodeBlock
                label="Quick setup commands"
                code={`handoff-verify init --write\nhandoff-verify doctor`}
              />
              <p className="text-[12.5px] leading-[1.55] text-[var(--t-ink-muted)]">
                Optional flags:{" "}
                <code className="font-mono text-[var(--t-ink-secondary)]">
                  --mode block
                </code>{" "}
                or{" "}
                <code className="font-mono text-[var(--t-ink-secondary)]">
                  --command &quot;uvx handoff-verify&quot;
                </code>
                .
              </p>
            </SurfaceTile>

            <SurfaceTile
              tone="neutral"
              bordered
              padding="md"
              className="lg:col-span-6 flex flex-col gap-4"
            >
              <div className="flex items-center gap-2">
                <ShieldCheck
                  className="h-5 w-5 text-[var(--t-success)]"
                  aria-hidden="true"
                />
                <h3 className="t-headline text-[20px]">
                  Hook safety guarantees
                </h3>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3.5">
                {HOOK_GUARANTEES.map((item) => (
                  <div
                    key={item.title}
                    className="rounded-[16px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-4 flex flex-col gap-1.5"
                  >
                    <h4 className="text-[14px] font-medium text-[var(--t-ink)]">
                      {item.title}
                    </h4>
                    <p className="text-[13px] leading-[1.5] text-[var(--t-ink-secondary)]">
                      {item.detail}
                    </p>
                  </div>
                ))}
              </div>
            </SurfaceTile>
          </div>

          {/* Supported Transcript Formats */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-5 pt-2">
            {TRANSCRIPT_FORMATS.map((fmt) => (
              <SurfaceTile
                key={fmt.id}
                tone={fmt.tone}
                bordered
                padding="md"
                className="flex flex-col justify-between gap-4"
              >
                <div className="flex flex-col gap-3">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="t-headline text-[19px]">{fmt.name}</h3>
                  </div>
                  <code className="inline-block self-start rounded-full bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] px-3 py-1 text-[11.5px] font-mono text-[var(--t-blue)]">
                    {fmt.parserFlag}
                  </code>
                  <p className="t-body text-[14px]">{fmt.description}</p>
                </div>
                <div className="pt-3 border-t border-[var(--t-border-subtle)]/80 font-mono text-[11.5px] text-[var(--t-ink-muted)] break-all">
                  {fmt.pathHint}
                </div>
              </SurfaceTile>
            ))}
          </div>
        </section>

        {/* Measured Benchmarks & Real-Session Calibration */}
        <section id="benchmarks" className="flex flex-col gap-6">
          <div className="max-w-2xl">
            <TonalChip tone="blue" className="mb-3">
              Measured Performance & Calibration
            </TonalChip>
            <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
              Sub-millisecond checks on small transcripts, calibrated on 1,571
              real sessions
            </h2>
            <p className="t-body text-[15px] text-[var(--t-ink-muted)]">
              Measured on macOS (<code className="font-mono text-[0.9em]">arm64</code>,
              Python 3.13.14) across the bundled 32-case seeded evaluation suite
              (16 clean / 16 planted failures via{" "}
              <code className="font-mono text-[0.9em]">handoff-verify eval</code>
              ), synthetic multi-megabyte transcripts, and a real-world corpus.
            </p>
          </div>

          <KpiGrid items={BENCHMARK_KPIS} columns={4} />

          <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
            {/* Scaling Table */}
            <SurfaceTile
              tone="white"
              bordered
              padding="md"
              className="lg:col-span-7 flex flex-col gap-4"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="t-headline text-[20px]">
                  Scaling & CLI wall-clock latency
                </h3>
                <TonalChip tone="neutral" className="font-mono text-[11px]">
                  up to 94.7 MB/s
                </TonalChip>
              </div>

              <div className="overflow-x-auto -mx-2 px-2">
                <table className="w-full text-left border-collapse text-[13px]">
                  <thead>
                    <tr className="border-b border-[var(--t-border-subtle)] text-[var(--t-ink-muted)]">
                      <th className="py-2.5 pr-3 font-medium">Workload</th>
                      <th className="py-2.5 px-2.5 font-medium text-right whitespace-nowrap">
                        Size
                      </th>
                      <th className="py-2.5 px-2.5 font-medium text-right whitespace-nowrap">
                        Parse
                      </th>
                      <th className="py-2.5 px-2.5 font-medium text-right whitespace-nowrap">
                        Verify
                      </th>
                      <th className="py-2.5 px-2.5 font-medium text-right whitespace-nowrap">
                        Median
                      </th>
                      <th className="py-2.5 pl-2.5 font-medium text-right whitespace-nowrap">
                        Notes
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--t-border-subtle)]/70 tabular-nums">
                    {SCALING_ROWS.map((row) => (
                      <tr key={row.workload}>
                        <td className="py-3 pr-3 text-[var(--t-ink)]">
                          {row.workload}
                        </td>
                        <td className="py-3 px-2.5 text-right font-mono text-[var(--t-ink-secondary)] whitespace-nowrap">
                          {row.size}
                        </td>
                        <td className="py-3 px-2.5 text-right font-mono text-[var(--t-ink-muted)] whitespace-nowrap">
                          {row.parse}
                        </td>
                        <td className="py-3 px-2.5 text-right font-mono text-[var(--t-ink-muted)] whitespace-nowrap">
                          {row.verify}
                        </td>
                        <td className="py-3 px-2.5 text-right font-mono font-medium text-[var(--t-ink)] whitespace-nowrap">
                          {row.total}
                        </td>
                        <td className="py-3 pl-2.5 text-right text-[12px] text-[var(--t-ink-muted)] whitespace-nowrap">
                          {row.throughput}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <p className="text-[12.5px] text-[var(--t-ink-muted)]">
                Reproduce locally with{" "}
                <code className="font-mono text-[var(--t-ink-secondary)]">
                  uv run handoff-verify eval --format markdown
                </code>
                . The 32-case suite is a deterministic regression benchmark
                authored alongside the verifier, not a held-out production
                corpus.
              </p>
            </SurfaceTile>

            {/* Real-Session Calibration */}
            <SurfaceTile
              tone="warm"
              bordered
              padding="md"
              className="lg:col-span-5 flex flex-col gap-4"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="t-headline text-[20px]">
                  Real-session calibration
                </h3>
                <TonalChip tone="neutral" className="text-[11px]">
                  1,571 transcripts
                </TonalChip>
              </div>

              <p className="text-[13.5px] leading-[1.55] text-[var(--t-ink-secondary)]">
                Run over 1,571 real Claude Code sessions from one developer&apos;s
                machine (972 main sessions, 599 subagents):
              </p>

              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-[16px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-3.5">
                  <div className="text-[11.5px] text-[var(--t-ink-muted)] mb-1">
                    Initial PyPI 0.1.0
                  </div>
                  <div className="text-[24px] font-medium tracking-[-0.03em] text-[var(--t-ink)] tabular-nums">
                    32.6%
                  </div>
                  <div className="text-[11.5px] font-mono text-[var(--t-ink-muted)]">
                    512 / 1,571 flagged
                  </div>
                </div>
                <div className="rounded-[16px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-3.5">
                  <div className="text-[11.5px] text-[var(--t-ink-muted)] mb-1">
                    After shell-read &amp; scratch-copy fixes
                  </div>
                  <div className="text-[24px] font-medium tracking-[-0.03em] text-[var(--t-success)] tabular-nums">
                    21.8%
                  </div>
                  <div className="text-[11.5px] font-mono text-[var(--t-ink-muted)]">
                    343 / 1,571 flagged
                  </div>
                </div>
              </div>

              <div className="flex flex-col gap-2 pt-1">
                <span className="text-[12px] font-medium text-[var(--t-ink)]">
                  Follow-up false-alarm classes fixed:
                </span>
                <ul className="flex flex-col gap-1.5">
                  {REAL_SESSION_FIXES.map((fix) => (
                    <li
                      key={fix}
                      className="flex items-start gap-2 text-[12.5px] leading-[1.45] text-[var(--t-ink-secondary)]"
                    >
                      <Check
                        className="h-3.5 w-3.5 shrink-0 mt-0.5 text-[var(--t-success)]"
                        aria-hidden="true"
                      />
                      <span>{fix}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </SurfaceTile>
          </div>

          {/* Honest Caveats */}
          <SurfaceTile
            tone="sand"
            bordered
            padding="md"
            className="flex flex-col sm:flex-row items-start gap-4"
          >
            <div className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] text-[var(--t-warning)]">
              <AlertTriangle className="h-5 w-5" aria-hidden="true" />
            </div>
            <div className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="t-headline text-[18px]">
                  Honest boundaries & caveats
                </h3>
                <TonalChip tone="warning" className="text-[11px]">
                  Start with advise mode
                </TonalChip>
              </div>
              <ul className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2 text-[13.5px] leading-[1.55] text-[var(--t-ink-secondary)]">
                <li>
                  Checks <strong className="font-medium text-[var(--t-ink)]">grounding against the transcript</strong>,
                  not whether code is correct or tests pass.
                </li>
                <li>
                  Treat any finding as{" "}
                  <em>&ldquo;the transcript does not show this&rdquo;</em>, not{" "}
                  <em>&ldquo;this is false&rdquo;</em>.
                </li>
                <li>
                  Reads via arbitrary inline scripts (e.g.{" "}
                  <code className="font-mono text-[0.9em]">python - &lt;&lt;EOF</code>{" "}
                  slicing or generating files) cannot always be mapped back to
                  exact line numbers.
                </li>
                <li>
                  <code className="font-mono text-[0.9em]">--latest</code>{" "}
                  currently discovers Claude Code session directories; pass
                  Codex CLI rollout paths explicitly.
                </li>
              </ul>
            </div>
          </SurfaceTile>
        </section>

        {/* FAQ */}
        <section id="faq" className="grid grid-cols-1 lg:grid-cols-12 gap-8">
          <div className="lg:col-span-4 flex flex-col gap-3">
            <TonalChip tone="neutral" className="self-start">
              FAQ
            </TonalChip>
            <h2 className="t-headline text-[28px] sm:text-[34px]">
              Practical questions
            </h2>
            <p className="t-body text-[14.5px] text-[var(--t-ink-muted)]">
              How deterministic verification fits alongside Claude Code hooks,
              Codex CLI rollouts, and CI pipelines.
            </p>
            <div className="pt-2">
              <ChevronLink
                href={REPO_URL}
                target="_blank"
                rel="noopener noreferrer"
              >
                Read full documentation on GitHub
              </ChevronLink>
            </div>
          </div>

          <div className="lg:col-span-8">
            <TonalAccordion items={FAQ_ITEMS} defaultOpenIndex={0} />
          </div>
        </section>
      </main>

      {/* Footer */}
      <footer className="border-t border-[var(--t-border-subtle)] mt-auto">
        <div className="mx-auto max-w-6xl px-5 sm:px-8 py-8 flex flex-col sm:flex-row items-center justify-between gap-4 text-[13px] text-[var(--t-ink-muted)]">
          <div className="flex items-center gap-2.5 text-[var(--t-ink)] font-medium">
            <BrandMark className="h-5 w-5" />
            <span>handoff-verify</span>
            <span className="text-[var(--t-ink-muted)] font-normal">
              · MIT License
            </span>
          </div>

          <div className="flex flex-wrap items-center justify-center gap-5">
            <a
              href={REPO_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-[var(--t-ink)] transition-colors"
            >
              GitHub
            </a>
            <a
              href={PYPI_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-[var(--t-ink)] transition-colors"
            >
              PyPI
            </a>
            <a
              href={HARLO_URL}
              className="hover:text-[var(--t-ink)] transition-colors"
            >
              harlo.dev
            </a>
            <span>
              Built with{" "}
              <a
                href={TONAL_URL}
                className="text-[var(--t-blue)] hover:underline underline-offset-4"
              >
                Tonal
              </a>
            </span>
          </div>
        </div>
      </footer>
    </div>
  );
}

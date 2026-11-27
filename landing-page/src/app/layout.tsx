import type { Metadata } from "next";
import "./globals.css";

const SITE_URL = "https://handoff-verify.harlo.dev";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: "handoff-verify — Deterministic Claim & Scope Verification for AI Agents",
    template: "%s — handoff-verify",
  },
  description:
    "Deterministic, zero-LLM CLI and Claude Code hook that verifies file pointers, verbatim code quotes, and search/pagination scope receipts against AI agent transcripts.",
  keywords: [
    "handoff-verify",
    "Claude Code hooks",
    "SubagentStop",
    "Codex CLI",
    "AI coding agents",
    "transcript verification",
    "deterministic verification",
    "Python CLI",
  ],
  alternates: {
    canonical: "/",
  },
  openGraph: {
    type: "website",
    url: SITE_URL,
    siteName: "handoff-verify",
    title: "handoff-verify — Zero-LLM Claim & Scope Verification for AI Agents",
    description:
      "Verify path:line pointers, quoted snippets, and 'no other callers' scope claims deterministically against Claude Code, Codex CLI, and generic JSONL transcripts.",
  },
  twitter: {
    card: "summary_large_image",
    title: "handoff-verify — Zero-LLM Claim & Scope Verification for AI Agents",
    description:
      "Deterministic Python 3.11+ CLI and Claude Code Stop/SubagentStop hook with zero runtime dependencies and sub-millisecond verification.",
  },
};

const THEME_INIT_SCRIPT = `(function(){try{var d=document.documentElement;var p=new URLSearchParams(window.location.search);var qT=p.get('theme');var t=(qT==='dark'||qT==='light')?qT:localStorage.getItem('handoff-verify-theme');if(t!=='dark'&&t!=='light'){t=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}d.dataset.tonalTheme=t;d.dataset.tonalMode='pill';}catch(e){}})();`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          rel="preconnect"
          href="https://fonts.gstatic.com"
          crossOrigin="anonymous"
        />
        <link
          href="https://fonts.googleapis.com/css2?family=Google+Sans:opsz,wght@17..18,400;17..18,500;17..18,700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}

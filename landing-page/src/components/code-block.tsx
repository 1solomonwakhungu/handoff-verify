"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";

export interface CodeBlockProps {
  code: string;
  copyText?: string;
  label?: string;
  badge?: React.ReactNode;
  className?: string;
  preClassName?: string;
}

export function CodeBlock({
  code,
  copyText,
  label,
  badge,
  className = "",
  preClassName = "",
}: CodeBlockProps) {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = React.useCallback(async () => {
    try {
      await navigator.clipboard.writeText(copyText ?? code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard unavailable
    }
  }, [code, copyText]);

  return (
    <div
      className={`rounded-[18px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] overflow-hidden ${className}`}
    >
      {(label || badge) && (
        <div className="flex items-center justify-between gap-3 px-4 py-2.5 border-b border-[var(--t-border-subtle)] bg-[var(--t-surface)]">
          <div className="flex items-center gap-2 min-w-0">
            {label && (
              <span className="text-[12px] font-mono text-[var(--t-ink-muted)] truncate">
                {label}
              </span>
            )}
            {badge}
          </div>
          <button
            type="button"
            onClick={handleCopy}
            aria-label={copied ? "Copied to clipboard" : `Copy ${label ?? "snippet"}`}
            className="inline-flex items-center gap-1.5 rounded-full bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] px-2.5 py-1 text-[11.5px] font-medium text-[var(--t-ink-secondary)] hover:text-[var(--t-ink)] hover:border-[var(--t-border)] cursor-pointer transition-colors shrink-0 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
          >
            {copied ? (
              <>
                <Check className="h-3.5 w-3.5 text-[var(--t-success)]" />
                <span>Copied</span>
              </>
            ) : (
              <>
                <Copy className="h-3.5 w-3.5" />
                <span>Copy</span>
              </>
            )}
          </button>
        </div>
      )}
      <div className="relative">
        {!label && !badge && (
          <button
            type="button"
            onClick={handleCopy}
            aria-label={copied ? "Copied to clipboard" : "Copy command"}
            className="absolute right-3 top-3 inline-flex h-8 w-8 items-center justify-center rounded-full bg-[var(--t-surface)] border border-[var(--t-border-subtle)] text-[var(--t-ink-muted)] hover:text-[var(--t-ink)] hover:border-[var(--t-border)] cursor-pointer transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
          >
            {copied ? (
              <Check className="h-3.5 w-3.5 text-[var(--t-success)]" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
          </button>
        )}
        <pre
          className={`overflow-x-auto p-4 text-[12.5px] sm:text-[13px] leading-[1.6] font-mono text-[var(--t-ink)] ${
            !label && !badge ? "pr-13" : ""
          } ${preClassName}`}
        >
          <code>{code}</code>
        </pre>
      </div>
    </div>
  );
}

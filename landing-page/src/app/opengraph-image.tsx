import { ImageResponse } from "next/og";

export const alt =
  "handoff-verify — Deterministic, zero-LLM claim and scope verification for AI coding agents";
export const size = {
  width: 1200,
  height: 630,
};
export const contentType = "image/png";

export default function OpenGraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          background: "#FFFFFF",
          padding: "56px 64px",
          fontFamily: "sans-serif",
          color: "#202124",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "14px" }}>
            <div
              style={{
                width: 40,
                height: 40,
                borderRadius: 12,
                background: "#131314",
                border: "1px solid rgba(255,255,255,0.1)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <svg
                width="28"
                height="28"
                viewBox="0 0 32 32"
                fill="none"
              >
                <path
                  d="M10.5 6H9.5A3.5 3.5 0 0 0 6 9.5V22.5A3.5 3.5 0 0 0 9.5 26H10.5"
                  stroke="#E3E3E3"
                  strokeWidth="4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <path
                  d="M21.5 6H22.5A3.5 3.5 0 0 1 26 9.5V22.5A3.5 3.5 0 0 1 22.5 26H21.5"
                  stroke="#E3E3E3"
                  strokeWidth="4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <path
                  d="M11.75 16.75L14.75 19.75L20.25 13.25"
                  stroke="#8AB4F8"
                  strokeWidth="4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </div>
            <span
              style={{
                fontSize: 30,
                fontWeight: 600,
                letterSpacing: "-0.025em",
              }}
            >
              handoff-verify
            </span>
          </div>
          <div
            style={{
              background: "#E8F0FE",
              color: "#174EA6",
              fontSize: 19,
              fontWeight: 600,
              padding: "10px 22px",
              borderRadius: 9999,
            }}
          >
            v0.1.1 · Zero-LLM · Python 3.11+ stdlib
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
          <div
            style={{
              fontSize: 60,
              fontWeight: 600,
              lineHeight: 1.07,
              letterSpacing: "-0.035em",
              maxWidth: 1020,
            }}
          >
            Verify AI agent handoffs against their transcripts.
          </div>
          <div
            style={{
              fontSize: 25,
              color: "#5F6368",
              lineHeight: 1.42,
              maxWidth: 920,
            }}
          >
            Deterministically check path:line pointers, verbatim quotes, and
            absence or pagination scope claims across Claude Code, Codex CLI,
            and generic JSONL logs.
          </div>
        </div>

        <div style={{ display: "flex", gap: "16px", alignItems: "stretch" }}>
          {[
            { label: "Precision (32-case)", value: "100.0%", bg: "#E6F4EA", accent: "#137333" },
            { label: "Median Latency", value: "0.60 ms", bg: "#E8F0FE", accent: "#174EA6" },
            { label: "Finding Codes", value: "12 stable", bg: "#F4F2EE", accent: "#3C4043" },
            { label: "Runtime Deps", value: "0 (stdlib)", bg: "#F0F4F9", accent: "#3C4043" },
          ].map((tile) => (
            <div
              key={tile.label}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "6px",
                flex: 1,
                background: tile.bg,
                borderRadius: 20,
                padding: "18px 22px",
              }}
            >
              <span
                style={{
                  fontSize: 15,
                  fontWeight: 500,
                  color: "#5F6368",
                }}
              >
                {tile.label}
              </span>
              <span
                style={{
                  fontSize: 28,
                  fontWeight: 600,
                  color: tile.accent,
                  letterSpacing: "-0.02em",
                }}
              >
                {tile.value}
              </span>
            </div>
          ))}
        </div>
      </div>
    ),
    { ...size }
  );
}

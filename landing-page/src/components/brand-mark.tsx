export function BrandMark({ className = "h-6 w-6" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      className={className}
    >
      <path
        d="M10.5 6H9.5A3.5 3.5 0 0 0 6 9.5V22.5A3.5 3.5 0 0 0 9.5 26H10.5"
        stroke="var(--t-ink)"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M21.5 6H22.5A3.5 3.5 0 0 1 26 9.5V22.5A3.5 3.5 0 0 1 22.5 26H21.5"
        stroke="var(--t-ink)"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M11.75 16.75L14.75 19.75L20.25 13.25"
        stroke="var(--t-blue)"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

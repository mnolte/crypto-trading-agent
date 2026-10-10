export function ErrorBadge({ count, href }: { count: number; href?: string }) {
  if (count <= 0) return null;
  return (
    <a
      href={href ?? "#"}
      title="Errors in the last 24h"
      className="error-badge"
    >
      <svg
        width="12"
        height="12"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        aria-hidden="true"
      >
        <line x1="12" y1="6" x2="12" y2="13" />
        <line x1="12" y1="18" x2="12" y2="18" />
      </svg>
      <span>
        {count} error{count === 1 ? "" : "s"} · 24h
      </span>
    </a>
  );
}

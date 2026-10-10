import { useEffect, type ReactNode } from "react";

function useEscape(onClose: (() => void) | undefined) {
  useEffect(() => {
    if (!onClose) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
}

function Panel({
  kind,
  label,
  meta,
  title,
  onClose,
  footer,
  children,
}: {
  kind: "drawer" | "side";
  label: string;
  meta?: ReactNode;
  title: ReactNode;
  onClose?: () => void;
  footer?: ReactNode;
  children: ReactNode;
}) {
  useEscape(onClose);
  return (
    <>
      <div className="overlay" onClick={onClose} aria-hidden="true" />
      <aside
        role="dialog"
        aria-label={label}
        className={`slide-panel slide-panel--${kind}`}
      >
        <div className="slide-panel__head">
          <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1, minWidth: 0 }}>
            {meta && <div className="slide-panel__meta">{meta}</div>}
            <h1 className="slide-panel__title">{title}</h1>
          </div>
          {onClose && (
            <button
              type="button"
              aria-label="Close"
              className="icon-btn"
              onClick={onClose}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                <line x1="6" y1="6" x2="18" y2="18" />
                <line x1="18" y1="6" x2="6" y2="18" />
              </svg>
            </button>
          )}
        </div>
        <div className="slide-panel__body">{children}</div>
        {footer && <div className="slide-panel__foot">{footer}</div>}
      </aside>
    </>
  );
}

/** Decision detail: 560 px drawer from the right. */
export function Drawer(props: Omit<Parameters<typeof Panel>[0], "kind">) {
  return <Panel kind="drawer" {...props} />;
}

/** Ask the agent: 520 px panel from the right over the dimmed screen. */
export function SidePanel(props: Omit<Parameters<typeof Panel>[0], "kind">) {
  return <Panel kind="side" {...props} />;
}

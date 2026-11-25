import { useEffect, useSyncExternalStore, useRef } from "react";

/** In-app toasts for notification events (design/DESIGN.md → Interaction
 * rules): one per event, newest on top, at most 3 visible. Info and
 * warning close after 10 s (hover pauses); critical stays until
 * dismissed. Browser notifications stay separate. */

export type ToastSeverity = "critical" | "warning" | "info";

export interface Toast {
  id: number;
  event: string;
  severity: ToastSeverity;
  title: string;
  body: string;
  at: number;
}

const MAX_VISIBLE = 3;
const AUTO_CLOSE_MS = 10_000;

let nextId = 1;
let toasts: Toast[] = [];
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

/** Push one toast per notification event: a repeat of the same event
 * replaces the earlier toast instead of stacking duplicates. */
export function pushToast(input: {
  event: string;
  severity: ToastSeverity;
  title: string;
  body: string;
}): void {
  toasts = [
    { ...input, id: nextId++, at: Date.now() },
    ...toasts.filter((t) => t.event !== input.event),
  ];
  emit();
}

export function dismissToast(id: number): void {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(subscribe, () => toasts);
}

export function ToastStack() {
  const all = useToasts();
  const visible = all.slice(0, MAX_VISIBLE);
  const hidden = all.length - visible.length;
  if (all.length === 0) return null;
  return (
    <div className="toast-stack" aria-label="Notifications">
      {visible.map((t) => (
        <ToastCard key={t.id} toast={t} />
      ))}
      {hidden > 0 && (
        <div className="toast toast--more">
          {hidden} more
        </div>
      )}
    </div>
  );
}

function ToastCard({ toast }: { toast: Toast }) {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const remaining = useRef(AUTO_CLOSE_MS);
  const started = useRef(0);
  const auto = toast.severity !== "critical";

  useEffect(() => {
    if (!auto) return;
    started.current = Date.now();
    timer.current = setTimeout(() => dismissToast(toast.id), remaining.current);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [auto, toast.id]);

  const pause = () => {
    if (!auto || !timer.current) return;
    clearTimeout(timer.current);
    timer.current = null;
    remaining.current -= Date.now() - started.current;
  };
  const resume = () => {
    if (!auto || timer.current) return;
    started.current = Date.now();
    timer.current = setTimeout(() => dismissToast(toast.id), remaining.current);
  };

  const cls = `toast toast--${toast.severity}`;
  return (
    <div
      role={toast.severity === "critical" ? "alert" : "status"}
      className={cls}
      onMouseEnter={pause}
      onMouseLeave={resume}
    >
      <div className="toast__title">{toast.title}</div>
      <div className="toast__body">{toast.body}</div>
      <div className="toast__actions">
        {toast.severity === "critical" && (
          <button type="button" className="toast__btn" onClick={() => dismissToast(toast.id)}>
            Dismiss
          </button>
        )}
        {auto && <span className="toast__hint">closes in 10 s</span>}
      </div>
    </div>
  );
}

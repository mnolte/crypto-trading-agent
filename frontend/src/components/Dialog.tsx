import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button, type ButtonVariant } from "./Button";
import { Input } from "./FormField";

export type DialogAction = {
  label: string;
  variant?: ButtonVariant;
  onClick?: () => void;
  disabled?: boolean;
  /** With `confirmText`, the action stays disabled until the word is typed. */
  requiresConfirm?: boolean;
};

/**
 * Modal dialog: centred, Escape closes, the destructive button is never the
 * default. `confirmText` adds a typed-confirmation input ("RESET", "DELETE").
 */
export function Dialog({
  title,
  children,
  actions,
  onClose,
  danger = false,
  confirmText,
  note,
}: {
  title: ReactNode;
  children?: ReactNode;
  actions: DialogAction[];
  onClose: () => void;
  danger?: boolean;
  confirmText?: string;
  note?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [typed, setTyped] = useState("");
  const confirmed = !confirmText || typed === confirmText;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    ref.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="overlay overlay--center"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        className={danger ? "dialog dialog--danger" : "dialog"}
        ref={ref}
        tabIndex={-1}
      >
        <h2 className="dialog__title">{title}</h2>
        {children && <div className="dialog__body">{children}</div>}
        {confirmText && (
          <label className="field">
            <span className="field__label">Type {confirmText} to confirm</span>
            <Input
              mono
              placeholder={confirmText}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
            />
          </label>
        )}
        <div className="dialog__actions">
          {actions.map((action) => (
            <Button
              key={action.label}
              variant={action.variant ?? "secondary"}
              disabled={action.disabled || (action.requiresConfirm === true && !confirmed)}
              onClick={action.onClick}
            >
              {action.label}
            </Button>
          ))}
        </div>
        {note && <p className="dialog__note">{note}</p>}
      </div>
    </div>
  );
}

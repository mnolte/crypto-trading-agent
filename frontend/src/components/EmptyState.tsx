import type { ReactNode } from "react";

/** Short text + what to do or when the content fills in. */
export function EmptyState({
  title,
  children,
  footer,
  actions,
}: {
  title: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty__title">{title}</span>
      {children && <span className="empty__body">{children}</span>}
      {footer && <span className="empty__foot">{footer}</span>}
      {actions && <div className="dev-row">{actions}</div>}
    </div>
  );
}

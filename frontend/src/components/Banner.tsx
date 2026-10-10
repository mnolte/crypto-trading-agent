import type { ReactNode } from "react";

export type BannerVariant = "info" | "warning" | "danger";

const ICONS: Record<BannerVariant, ReactNode> = {
  info: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true" className="banner__icon">
      <circle cx="12" cy="12" r="9" />
      <line x1="12" y1="11" x2="12" y2="16" />
      <line x1="12" y1="8" x2="12" y2="8" />
    </svg>
  ),
  warning: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="banner__icon">
      <path d="M12 3l10 18H2z" />
      <line x1="12" y1="10" x2="12" y2="14" />
      <line x1="12" y1="18" x2="12" y2="18" />
    </svg>
  ),
  danger: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="banner__icon">
      <circle cx="12" cy="12" r="9" />
      <line x1="12" y1="7" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12" y2="17" />
    </svg>
  ),
};

export function Banner({
  variant,
  children,
  actions,
}: {
  variant: BannerVariant;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className={`banner banner--${variant}`} role={variant === "danger" ? "alert" : "status"}>
      {ICONS[variant]}
      <span className="banner__text">{children}</span>
      {actions}
    </div>
  );
}

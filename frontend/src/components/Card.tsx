import type { ReactNode } from "react";

/** Panel + optional heading + header actions.
 * `eyebrow` is the small uppercase muted heading; `section` the larger
 * heading used on Settings. */
export function Card({
  title,
  heading = "eyebrow",
  actions,
  children,
  variant,
  label,
  className = "",
  style,
}: {
  title?: ReactNode;
  heading?: "eyebrow" | "section";
  actions?: ReactNode;
  children: ReactNode;
  variant?: "accent" | "accent-dim" | "danger";
  label?: string;
  className?: string;
  style?: React.CSSProperties;
}) {
  const cls = [
    "card",
    variant ? `card--${variant}` : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <section aria-label={label ?? (typeof title === "string" ? title : undefined)} className={cls} style={style}>
      {(title || actions) && (
        <div className="card__head">
          {title && (
            <h2
              className={
                heading === "section"
                  ? "card__title card__title--section"
                  : "card__title"
              }
            >
              {title}
            </h2>
          )}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

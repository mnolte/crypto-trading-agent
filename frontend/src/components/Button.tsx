import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "danger"
  | "danger-outline"
  | "danger-solid"
  | "ghost";

export type ButtonSize = "md" | "sm" | "xs";

export function Button({
  variant = "secondary",
  size = "md",
  icon,
  children,
  className = "",
  ...rest
}: {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: ReactNode;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  const sizeClass = size === "md" ? "" : ` btn--${size}`;
  return (
    <button
      type="button"
      className={`btn btn--${variant}${sizeClass} ${className}`.trim()}
      {...rest}
    >
      {icon}
      {children !== undefined && <span>{children}</span>}
    </button>
  );
}

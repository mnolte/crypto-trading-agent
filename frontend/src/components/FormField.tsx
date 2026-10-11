import { useId, type ReactNode } from "react";

/** Label above a 44 px input, with unit, hint and validation message. */
export function FormField({
  label,
  children,
  unit,
  error,
  hint,
  htmlFor,
  className = "",
}: {
  label: ReactNode;
  children?: ReactNode;
  unit?: string;
  error?: string;
  hint?: string;
  htmlFor?: string;
  className?: string;
}) {
  const autoId = useId();
  return (
    <label className={`field ${className}`.trim()} htmlFor={htmlFor ?? autoId}>
      <span className="field__label">{label}</span>
      <span className="field__control">
        {children}
        {unit && <span className="field__unit">{unit}</span>}
      </span>
      {error && <span className="field__msg">{error}</span>}
      {hint && !error && <span className="field__hint">{hint}</span>}
    </label>
  );
}

export type InputProps = React.InputHTMLAttributes<HTMLInputElement> & {
  mono?: boolean;
  invalid?: boolean;
};

export function Input({ mono, invalid, className = "", ...rest }: InputProps) {
  const cls = [
    "input",
    mono ? "input--mono" : "",
    invalid ? "input--invalid" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return <input className={cls} {...rest} />;
}

export function Select({
  invalid,
  className = "",
  ...rest
}: React.SelectHTMLAttributes<HTMLSelectElement> & { invalid?: boolean }) {
  return (
    <select
      className={`input${invalid ? " input--invalid" : ""} ${className}`.trim()}
      {...rest}
    />
  );
}

/** Checkbox with its label text, 44 px hit target. */
export function CheckField({
  label,
  sub,
  ...rest
}: React.InputHTMLAttributes<HTMLInputElement> & {
  label: ReactNode;
  sub?: string;
}) {
  return (
    <label className="check-label">
      <input type="checkbox" {...rest} />
      <span>
        {label}
        {sub && (
          <>
            <br />
            <span style={{ fontSize: "var(--fs-caption)", color: "var(--text-muted)" }}>
              {sub}
            </span>
          </>
        )}
      </span>
    </label>
  );
}

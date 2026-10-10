export type SegmentedOption = {
  value: string;
  label: string;
  disabled?: boolean;
  title?: string;
};

/** One selected segment. `boxed` is the Paper/Live style; `flat` the 1h/4h/1d
 * toggle inside cards. */
export function SegmentedControl({
  options,
  value,
  onChange,
  label,
  flat = false,
}: {
  options: SegmentedOption[];
  value: string;
  onChange?: (value: string) => void;
  label: string;
  flat?: boolean;
}) {
  return (
    <div role="group" aria-label={label} className={flat ? "seg seg--flat" : "seg"}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={
            option.value === value ? "seg__btn seg__btn--on" : "seg__btn"
          }
          disabled={option.disabled}
          title={option.title}
          onClick={() => onChange?.(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

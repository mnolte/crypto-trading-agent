import { useState } from "react";
import type { EntryPause } from "../api/client";
import { usePauseEntries, useResumeEntries } from "../api/hooks";
import { formatDateTime } from "../lib/format";

/**
 * "Pause new entries" select for one pair — or all coins with pair "*".
 * Options mirror the spec: not paused, 1 hour, 24 hours, a chosen date and
 * time, or until the user resumes.
 */
export function PauseSelect({
  pair,
  pause,
}: {
  pair: string;
  pause: EntryPause | undefined;
}) {
  const pauseEntries = usePauseEntries();
  const resumeEntries = useResumeEntries();
  const [dateMode, setDateMode] = useState(false);
  const [dateVal, setDateVal] = useState("");
  const [error, setError] = useState<string | null>(null);

  const busy = pauseEntries.isPending || resumeEntries.isPending;
  const paused = pause !== undefined;
  const until = pause?.until;
  const onError = (e: unknown) =>
    setError(e instanceof Error ? e.message : String(e));

  const value = !paused
    ? "none"
    : until === null
      ? "indefinite"
      : dateMode
        ? "date"
        : "until";

  const set = (iso: string | null) =>
    pauseEntries.mutate({ pair, until: iso }, { onError });

  return (
    <span className="pause-select">
      <select
        className="input input--sm"
        aria-label={pair === "*" ? "Pause new entries on all coins" : `Pause new entries on ${pair}`}
        value={value}
        disabled={busy}
        onChange={(e) => {
          const v = e.target.value;
          setError(null);
          if (v === "none") resumeEntries.mutate(pair, { onError });
          else if (v === "1h") set(new Date(Date.now() + 3_600_000).toISOString());
          else if (v === "24h") set(new Date(Date.now() + 86_400_000).toISOString());
          else if (v === "date") setDateMode(true);
          else if (v === "indefinite") set(null);
          // "until" is the current fixed-date state — reselecting it is a no-op
        }}
      >
        <option value="none">Not paused</option>
        <option value="1h">1 hour</option>
        <option value="24h">24 hours</option>
        <option value="date">Until a date…</option>
        <option value="indefinite">Until I resume</option>
        {until !== null && until !== undefined && (
          <option value="until">Until {formatDateTime(until)}</option>
        )}
      </select>
      {dateMode && (
        <input
          type="datetime-local"
          className="input input--sm"
          aria-label="Pause until"
          value={dateVal}
          onChange={(e) => setDateVal(e.target.value)}
          onBlur={() => {
            if (dateVal) set(new Date(dateVal).toISOString());
            setDateMode(false);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            if (e.key === "Escape") setDateMode(false);
          }}
        />
      )}
      {error && <span className="pause-select__error">{error}</span>}
    </span>
  );
}

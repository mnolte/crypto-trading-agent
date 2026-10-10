import { useEffect, useRef, useState } from "react";
import { formatChange } from "../lib/format";

export type PairTabItem = {
  id: string;
  chg24h: number;
  /** e.g. "18h" for a per-coin pause */
  paused?: string;
  stale?: boolean;
};

const MAIN_COUNT = 6;

/** First 6 active pairs as tabs; the rest live in a searchable More menu. */
export function PairTabs({
  pairs,
  value,
  onChange,
}: {
  pairs: PairTabItem[];
  value: string;
  onChange: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const main = pairs.slice(0, MAIN_COUNT);
  const rest = pairs.slice(MAIN_COUNT);
  const selInMore = rest.some((p) => p.id === value);
  const filtered = rest.filter((p) =>
    p.id.toLowerCase().includes(query.toLowerCase()),
  );

  return (
    <div className="pairtabs" ref={rootRef}>
      <div role="tablist" aria-label="Pairs" className="pairtabs__list">
        {main.map((p) => (
          <PairTab key={p.id} pair={p} on={p.id === value} onPick={onChange} />
        ))}
      </div>
      {rest.length > 0 && (
        <button
          type="button"
          aria-haspopup="listbox"
          aria-expanded={open}
          className={selInMore ? "pairtab pairtab--on" : "pairtab"}
          onClick={() => setOpen((v) => !v)}
        >
          <span>{selInMore ? value : `More (${rest.length})`}</span>
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
      )}
      {open && (
        <div role="listbox" aria-label="More pairs" className="pairtabs__menu">
          <input
            type="search"
            aria-label="Search pairs"
            placeholder="Search pairs"
            className="pairtabs__search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
          {filtered.map((p) => (
            <button
              key={p.id}
              type="button"
              role="option"
              aria-selected={p.id === value}
              className={
                p.id === value ? "pairtab-option pairtab-option--on" : "pairtab-option"
              }
              onClick={() => {
                onChange(p.id);
                setOpen(false);
              }}
            >
              <span>
                {p.id}
                {p.paused && (
                  <span className="pairtab__badge">paused {p.paused}</span>
                )}
              </span>
              <Change value={p.chg24h} />
            </button>
          ))}
          {filtered.length === 0 && (
            <span className="empty__body" style={{ padding: "8px 10px" }}>
              No matches
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function Change({ value }: { value: number }) {
  return (
    <span className={`pairtab__chg pairtab__chg--${value >= 0 ? "up" : "down"}`}>
      {formatChange(value)}
    </span>
  );
}

function PairTab({
  pair,
  on,
  onPick,
}: {
  pair: PairTabItem;
  on: boolean;
  onPick: (id: string) => void;
}) {
  const cls = [
    "pairtab",
    on ? "pairtab--on" : "",
    pair.stale ? "pairtab--warn" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      type="button"
      role="tab"
      aria-selected={on}
      className={cls}
      onClick={() => onPick(pair.id)}
    >
      {pair.stale && <span className="pairtab__dot" aria-hidden="true" />}
      <span>{pair.id}</span>
      {pair.stale ? (
        <span className="pairtab__chg pairtab__chg--warn num">stale</span>
      ) : (
        <Change value={pair.chg24h} />
      )}
      {pair.paused && <span className="pairtab__badge">paused {pair.paused}</span>}
    </button>
  );
}

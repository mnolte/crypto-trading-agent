import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

/** The in-app error pages, using the design's wording. */

export function NotFoundPage() {
  return (
    <div className="err-page">
      <p className="err-page__badge">404 · not found</p>
      <h1 className="err-page__title">There is no page here</h1>
      <p>Check the address, or pick a screen from the menu.</p>
      <div className="err-page__actions">
        <Link to="/">Go to Dashboard</Link>
      </div>
    </div>
  );
}

export function RecordNotFound({ kind, id }: { kind: string; id: string }) {
  return (
    <div className="err-page">
      <p className="err-page__badge">404 · not found</p>
      <h1 className="err-page__title">
        {kind} {id} doesn't exist
      </h1>
      <p>
        It may be from a paper account that was reset, or the link is
        mistyped. Records are never deleted within a paper account.
      </p>
      <div className="err-page__actions">
        <Link to="/">Go to Dashboard</Link>
      </div>
    </div>
  );
}

/** First load only: the API is not answering at all. Once the app has
 * loaded once, the backend banner in the shell takes over instead. */
export function AppNotAnswering({ onRetry }: { onRetry: () => void }) {
  const [seconds, setSeconds] = useState(15);
  useEffect(() => {
    const t = setInterval(() => setSeconds((s) => (s <= 1 ? 15 : s - 1)), 1000);
    const r = setInterval(onRetry, 15_000);
    return () => {
      clearInterval(t);
      clearInterval(r);
    };
  }, [onRetry]);
  return (
    <div className="err-page err-page--center">
      <p className="err-page__badge">no response · retrying in {seconds} s</p>
      <h1 className="err-page__title">The app isn't answering</h1>
      <p>
        This page loaded, but the agent's API didn't respond. It may be
        restarting. Trying again every 15 seconds.
      </p>
      <p className="err-page__hint">
        Positions are paper only. Stops and targets are checked only
        while the app runs; it checks them against the current price as
        soon as it is back.
      </p>
      <div className="err-page__actions">
        <button type="button" onClick={onRetry}>
          Try now
        </button>
      </div>
    </div>
  );
}

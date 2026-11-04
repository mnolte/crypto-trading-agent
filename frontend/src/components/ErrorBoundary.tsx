import { Component, type ErrorInfo, type ReactNode } from "react";
import { reportClient } from "../lib/reporting";

type Props = { children: ReactNode };
type State = { errorId: string | null };

/** Per-screen error boundary. The design's wording: "Something went
 * wrong on this page … The agent itself is not affected and keeps
 * trading. The error was logged with the id above." */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { errorId: null };

  static getDerivedStateFromError(): State {
    return { errorId: "" };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    const id = reportClient("error", `${error.name}: ${error.message}`, {
      stack: (error.stack ?? "").slice(0, 3000),
      componentStack: (info.componentStack ?? "").slice(0, 3000),
      via: "error-boundary",
    });
    this.setState({ errorId: id });
  }

  render() {
    if (this.state.errorId === null) return this.props.children;
    const id = this.state.errorId;
    return (
      <div className="err-page">
        <p className="err-page__badge">Something went wrong{id ? ` · error ${id}` : ""}</p>
        <h1 className="err-page__title">Something went wrong on this page</h1>
        <p>
          The page hit an error and stopped drawing. The agent itself is
          not affected and keeps trading. The error was logged with the
          id above.
        </p>
        <p className="err-page__hint">
          Only this screen failed; the sidebar and the other screens
          still work.
        </p>
        <div className="err-page__actions">
          <button type="button" onClick={() => location.reload()}>
            Reload page
          </button>
          {id ? (
            <button
              type="button"
              onClick={() => void navigator.clipboard.writeText(id)}
            >
              Copy error id
            </button>
          ) : null}
          {id ? <a href={`/logs?q=${encodeURIComponent(id)}`}>Open in Logs</a> : null}
        </div>
      </div>
    );
  }
}

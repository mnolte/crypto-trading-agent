import { useState, type ReactNode } from "react";
import { useSnapshot, useSnapshotStream } from "../api/hooks";
import { displayStatus } from "../lib/status";
import { EmptyState } from "./EmptyState";
import { Sidebar } from "./Sidebar";
import { SidePanel } from "./SlidePanel";
import { TopBar } from "./TopBar";

/**
 * Collapsible sidebar + top bar + centred page (max 1440 px). The Ask button
 * opens the read-only Ask side panel over the current screen.
 */
export function AppShell({
  title,
  actions,
  controls = true,
  children,
}: {
  title: string;
  actions?: ReactNode;
  controls?: boolean;
  children?: ReactNode;
}) {
  const { connected } = useSnapshotStream();
  const { data: snapshot, dataUpdatedAt } = useSnapshot();
  const status = displayStatus(connected, snapshot?.status);
  const lastKnown = snapshot ? { status: snapshot.status, at: dataUpdatedAt } : undefined;
  const [askOpen, setAskOpen] = useState(false);

  return (
    <div className="shell">
      <Sidebar status={status} lastKnown={lastKnown} onAsk={() => setAskOpen(true)} />
      <div className="shell__main">
        <TopBar
          title={title}
          status={status}
          lastKnown={lastKnown}
          running={status === "running"}
          errors24h={snapshot?.errors_24h ?? 0}
          controls={controls}
          actions={actions}
        />
        <main className="page">{children}</main>
      </div>
      {askOpen && (
        <SidePanel
          label="Ask the agent"
          title="Ask the agent"
          onClose={() => setAskOpen(false)}
        >
          <EmptyState title="Ask the agent" footer="This panel arrives with the Evidence build step." />
        </SidePanel>
      )}
    </div>
  );
}

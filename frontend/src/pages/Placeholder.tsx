import { AppShell } from "../components/AppShell";
import { EmptyState } from "../components/EmptyState";

/** Sidebar entry for a screen that a later build step fills in. */
export function Placeholder({ title, step }: { title: string; step: string }) {
  return (
    <AppShell title={title}>
      <EmptyState title={title}>{`This screen arrives with the ${step} build step.`}</EmptyState>
    </AppShell>
  );
}

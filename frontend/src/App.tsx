import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { GlossaryProvider } from "./components/Glossary";
import { AppNotAnswering, NotFoundPage } from "./pages/Errors";

const Dashboard = lazy(() =>
  import("./pages/Dashboard").then((m) => ({ default: m.Dashboard })),
);
const DevComponents = lazy(() =>
  import("./pages/DevComponents").then((m) => ({ default: m.DevComponents })),
);
const Indicators = lazy(() =>
  import("./pages/Indicators").then((m) => ({ default: m.Indicators })),
);
const Performance = lazy(() =>
  import("./pages/Performance").then((m) => ({ default: m.Performance })),
);
const Inspector = lazy(() =>
  import("./pages/Inspector").then((m) => ({ default: m.Inspector })),
);
const Readiness = lazy(() =>
  import("./pages/Readiness").then((m) => ({ default: m.Readiness })),
);
const Logs = lazy(() =>
  import("./pages/Logs").then((m) => ({ default: m.Logs })),
);
const Settings = lazy(() =>
  import("./pages/Settings").then((m) => ({ default: m.Settings })),
);
const Insights = lazy(() =>
  import("./pages/Insights").then((m) => ({ default: m.Insights })),
);
const WhatIf = lazy(() =>
  import("./pages/WhatIf").then((m) => ({ default: m.WhatIf })),
);
const Reviews = lazy(() =>
  import("./pages/Reviews").then((m) => ({ default: m.Reviews })),
);

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

/** On the very first load, a silent API means the app behind the SPA is
 * down — the design's "The app isn't answering" page, retried every
 * 15 s. After a first load the backend banner takes over instead. */
function HealthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<"checking" | "up" | "down">("checking");
  const check = useCallback(() => {
    fetch("/api/session", { signal: AbortSignal.timeout(10_000) })
      .then((r) => setState(r.ok ? "up" : "down"))
      .catch(() => setState("down"));
  }, []);
  useEffect(check, [check]);
  if (state === "down") return <AppNotAnswering onRetry={check} />;
  if (state === "checking") return null;
  return <>{children}</>;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <GlossaryProvider>
          <HealthGate>
            <Suspense fallback={null}>
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/settings" element={<Settings />} />
                <Route path="/dev/components" element={<DevComponents />} />
                <Route path="/indicators" element={<Indicators />} />
                <Route path="/performance" element={<Performance />} />
                <Route path="/insights" element={<Insights />} />
                <Route path="/what-if" element={<WhatIf />} />
                <Route path="/reviews" element={<Reviews />} />
                <Route path="/inspector" element={<Inspector />} />
                <Route path="/readiness" element={<Readiness />} />
                <Route path="/logs" element={<Logs />} />
                <Route path="*" element={<NotFoundPage />} />
              </Routes>
            </Suspense>
          </HealthGate>
        </GlossaryProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

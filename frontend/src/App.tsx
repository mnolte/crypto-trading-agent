import { lazy, Suspense } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Placeholder } from "./pages/Placeholder";

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
const Settings = lazy(() =>
  import("./pages/Settings").then((m) => ({ default: m.Settings })),
);

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Suspense fallback={null}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/dev/components" element={<DevComponents />} />
            <Route path="/indicators" element={<Indicators />} />
            <Route path="/performance" element={<Performance />} />
            <Route
              path="/insights"
              element={<Placeholder title="Insights" step="Evidence" />}
            />
            <Route
              path="/what-if"
              element={<Placeholder title="What-if" step="Evidence" />}
            />
            <Route
              path="/reviews"
              element={<Placeholder title="Reviews" step="Evidence" />}
            />
            <Route
              path="/inspector"
              element={<Placeholder title="LLM inspector" step="OpenAI decider" />}
            />
            <Route
              path="/readiness"
              element={<Placeholder title="Readiness" step="Understanding and safety" />}
            />
          </Routes>
        </Suspense>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

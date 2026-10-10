import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Dashboard } from "./pages/Dashboard";
import { DevComponents } from "./pages/DevComponents";
import { Placeholder } from "./pages/Placeholder";
import { Settings } from "./pages/Settings";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/dev/components" element={<DevComponents />} />
          <Route
            path="/indicators"
            element={<Placeholder title="Indicators" step="API and GUI" />}
          />
          <Route
            path="/performance"
            element={<Placeholder title="Performance" step="API and GUI" />}
          />
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
      </BrowserRouter>
    </QueryClientProvider>
  );
}

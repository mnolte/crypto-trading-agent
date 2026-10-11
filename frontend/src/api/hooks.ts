import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  api,
  post,
  type CandleFeed,
  type CostsOut,
  type DecisionDetail,
  type DecisionListOut,
  type FlattenPreview,
  type HealthOut,
  type IndicatorsOut,
  type LlmCallDetail,
  type LlmCallListOut,
  type MarketOut,
  type NotificationSettings,
  type OutcomeScorecardOut,
  type PerformanceOut,
  type PromptInfo,
  type Settings,
  type SettingsOut,
  type Snapshot,
  type SourceOut,
  type Tf,
  type TradeListOut,
} from "./client";

export function useSnapshot() {
  return useQuery({
    queryKey: ["snapshot"],
    queryFn: () => api<Snapshot>("/api/snapshot"),
    staleTime: 60_000,
  });
}

export function useSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: () => api<SettingsOut>("/api/settings"),
    staleTime: 60_000,
  });
}

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => api<HealthOut>("/api/health"),
    staleTime: 30_000,
  });
}

export function useCandles(pair: string | null, tf: Tf, bars = 300) {
  return useQuery({
    queryKey: ["candles", pair, tf, bars],
    queryFn: () => api<CandleFeed>(`/api/candles/${pair}?tf=${tf}&bars=${bars}`),
    enabled: pair !== null,
    staleTime: 30_000,
  });
}

export function useDecisions(pair?: string, limit = 50) {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (pair) qs.set("pair", pair);
  return useQuery({
    queryKey: ["decisions", pair ?? "all", limit],
    queryFn: () => api<DecisionListOut>(`/api/decisions?${qs}`),
    staleTime: 15_000,
  });
}

export function useDecision(id: string | null) {
  return useQuery({
    queryKey: ["decision", id],
    queryFn: () => api<DecisionDetail>(`/api/decisions/${id}`),
    enabled: id !== null,
  });
}

export function useIndicators(pair: string | null, tf: Tf, at?: string | null) {
  const qs = new URLSearchParams({ tf });
  if (at) qs.set("at", at);
  return useQuery({
    queryKey: ["indicators", pair, tf, at ?? "live"],
    queryFn: () => api<IndicatorsOut>(`/api/indicators/${pair}?${qs}`),
    enabled: pair !== null,
    staleTime: 30_000,
  });
}

export function usePerformance(from?: string, end?: string) {
  const qs = new URLSearchParams();
  if (from) qs.set("from", from);
  if (end) qs.set("end", end);
  return useQuery({
    queryKey: ["performance", from ?? "start", end ?? ""],
    queryFn: () => api<PerformanceOut>(`/api/performance?${qs}`),
    staleTime: 30_000,
  });
}

export function useTrades(from?: string, end?: string) {
  const qs = new URLSearchParams();
  if (from) qs.set("from", from);
  if (end) qs.set("end", end);
  return useQuery({
    queryKey: ["trades", from ?? "", end ?? ""],
    queryFn: () => api<TradeListOut>(`/api/trades?${qs}`),
    staleTime: 30_000,
  });
}

export function useMarkets() {
  return useQuery({
    queryKey: ["markets"],
    queryFn: () => api<MarketOut[]>("/api/markets"),
    staleTime: 60_000,
  });
}

export function useSources() {
  return useQuery({
    queryKey: ["sources"],
    queryFn: () => api<SourceOut[]>("/api/sources"),
    staleTime: 30_000,
  });
}

export function useLlmCalls(filters: {
  pair?: string;
  status?: string;
  verdict?: string;
  outcome?: string;
  issues?: boolean;
  skipped?: boolean;
  limit?: number;
}) {
  const qs = new URLSearchParams();
  if (filters.pair) qs.set("pair", filters.pair);
  if (filters.status) qs.set("status", filters.status);
  if (filters.verdict) qs.set("verdict", filters.verdict);
  if (filters.outcome) qs.set("outcome", filters.outcome);
  if (filters.issues) qs.set("issues", "true");
  if (filters.skipped === false) qs.set("skipped", "false");
  if (filters.limit) qs.set("limit", String(filters.limit));
  return useQuery({
    queryKey: ["llm-calls", filters],
    queryFn: () => api<LlmCallListOut>(`/api/llm-calls?${qs}`),
    staleTime: 10_000,
  });
}

export function useLlmCall(id: string | null) {
  return useQuery({
    queryKey: ["llm-call", id],
    queryFn: () => api<LlmCallDetail>(`/api/llm-calls/${id}`),
    enabled: id !== null,
  });
}

export function useOutcomeScorecard() {
  return useQuery({
    queryKey: ["outcome-scorecard"],
    queryFn: () => api<OutcomeScorecardOut>("/api/outcomes/scorecard"),
    staleTime: 60_000,
  });
}

export function useCosts(month?: string) {
  const qs = month ? `?month=${month}` : "";
  return useQuery({
    queryKey: ["costs", month ?? "current"],
    queryFn: () => api<CostsOut>(`/api/costs${qs}`),
    staleTime: 60_000,
  });
}

export function useReplayCall() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      model,
      promptVersion,
    }: {
      id: string;
      model?: string;
      promptVersion?: string;
    }) =>
      api<LlmCallDetail>(`/api/llm-calls/${id}/replay`, {
        method: "POST",
        body: JSON.stringify({
          model: model || null,
          prompt_version: promptVersion || null,
        }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["llm-calls"] });
      void client.invalidateQueries({ queryKey: ["llm-call"] });
    },
  });
}

export function useSetVerdict() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      verdict,
      note,
    }: {
      id: string;
      verdict: "correct" | "wrong" | null;
      note?: string;
    }) =>
      api<LlmCallDetail>(`/api/llm-calls/${id}/verdict`, {
        method: "PUT",
        body: JSON.stringify({ verdict, note: note ?? null }),
      }),
    onSuccess: (detail) => {
      client.setQueryData(["llm-call", detail.id], detail);
      void client.invalidateQueries({ queryKey: ["llm-calls"] });
    },
  });
}

export function useNotifications() {
  return useQuery({
    queryKey: ["notifications"],
    queryFn: () => api<NotificationSettings>("/api/notifications"),
    staleTime: 60_000,
  });
}

export function useSaveSmtpPassword() {
  return useMutation({
    mutationFn: (password: string) =>
      api<{ set: boolean; last4: string | null }>("/api/notifications/smtp-password", {
        method: "PUT",
        body: JSON.stringify({ password }),
      }),
  });
}

export function useTestNotifications() {
  return useMutation({
    mutationFn: () => post<{ browser: boolean; email: boolean }>("/api/notifications/test"),
  });
}

export function usePrompts() {
  return useQuery({
    queryKey: ["prompts"],
    queryFn: () => api<PromptInfo[]>("/api/prompts"),
    staleTime: 300_000,
  });
}

export function useFlattenPreview(enabled: boolean) {
  return useQuery({
    queryKey: ["flatten-preview"],
    queryFn: () => api<FlattenPreview>("/api/agent/flatten-preview"),
    enabled,
  });
}

/** A snapshot-returning agent control; keeps the query cache in sync. */
function useAgentAction(path: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => post<Snapshot>(path),
    onSuccess: (snapshot) => client.setQueryData(["snapshot"], snapshot),
  });
}

export function usePauseAgent() {
  return useAgentAction("/api/agent/pause");
}
export function useResumeAgent() {
  return useAgentAction("/api/agent/resume");
}
export function useRunNow() {
  return useAgentAction("/api/agent/run-now");
}
export function useFlatten() {
  return useAgentAction("/api/agent/flatten");
}
export function useAcknowledgeKill() {
  return useAgentAction("/api/agent/acknowledge-kill");
}
export function useResetPaper() {
  return useAgentAction("/api/paper/reset");
}

export function usePendingAction(action: "approve" | "veto") {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      post<Snapshot>(`/api/decisions/${id}/${action}`),
    onSuccess: (snapshot) => client.setQueryData(["snapshot"], snapshot),
  });
}

export function useClosePosition() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (pair: string) => post<Snapshot>(`/api/positions/${pair}/close`),
    onSuccess: (snapshot) => client.setQueryData(["snapshot"], snapshot),
  });
}

export function useSaveSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (settings: Settings) =>
      api<SettingsOut>("/api/settings", {
        method: "PUT",
        body: JSON.stringify(settings),
      }),
    onSuccess: (data) => client.setQueryData(["settings"], data),
  });
}

export function useUpdateMarket() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ market, active }: { market: string; active: boolean }) =>
      api<MarketOut>(`/api/markets/${encodeURIComponent(market)}`, {
        method: "PUT",
        body: JSON.stringify({ active }),
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["markets"] }),
  });
}

export function useUpdateSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api<SourceOut>(`/api/sources/${id}`, {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["sources"] }),
  });
}

export function useSaveSecret() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, key }: { id: string; key: string }) =>
      api<{ ok: boolean }>(`/api/secrets/${id}`, {
        method: "PUT",
        body: JSON.stringify({ key }),
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["sources"] }),
  });
}

export function useRemoveSecret() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api<{ ok: boolean }>(`/api/secrets/${id}`, { method: "DELETE" }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["sources"] }),
  });
}

export function useTestSecret() {
  return useMutation({
    mutationFn: (id: string) => post<{ ok: boolean; error: string | null }>(`/api/secrets/${id}/test`),
  });
}

export function useDeleteData() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api<{ ok: boolean }>("/api/data", {
        method: "DELETE",
        body: JSON.stringify({ confirm: "DELETE" }),
      }),
    onSuccess: () => client.invalidateQueries(),
  });
}

/** A wall clock that re-renders every `stepMs`; used for countdowns. */
export function useNow(stepMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), stepMs);
    return () => window.clearInterval(id);
  }, [stepMs]);
  return now;
}

/**
 * Subscribe to the snapshot WebSocket. The server pushes {"type":"snapshot"}
 * after every change; on reconnect (with backoff) we refetch the snapshot.
 *
 * `connected` is true only after a fresh snapshot arrived on the current
 * socket — the UI shows "Disconnected" the moment the stream drops.
 */
export function useSnapshotStream(): { connected: boolean } {
  const client = useQueryClient();
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    let ws: WebSocket | null = null;
    let attempt = 0;
    let closed = false;
    let timer: number | undefined;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        attempt = 0;
      };
      ws.onmessage = (event) => {
        const message = JSON.parse(event.data as string) as {
          type: string;
          data?: Snapshot | { title: string; body: string };
        };
        if (message.type === "snapshot" && message.data) {
          setConnected(true);
          client.setQueryData(["snapshot"], message.data as Snapshot);
        } else if (
          message.type === "alert" &&
          message.data &&
          "title" in message.data &&
          "Notification" in window &&
          Notification.permission === "granted"
        ) {
          new Notification(message.data.title, { body: message.data.body });
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (closed) return;
        attempt += 1;
        const delay = Math.min(30_000, 500 * 2 ** attempt);
        timer = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      closed = true;
      setConnected(false);
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [client]);
  return { connected };
}

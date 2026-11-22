import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { pushToast } from "../lib/toasts";
import {
  api,
  post,
  type AskConversationListOut,
  type AskConversationOut,
  type ActivityListOut,
  type AskIn,
  type AskOut,
  type BriefingListOut,
  type CandleFeed,
  type CostsOut,
  type CyclesRecent,
  type DecisionDetail,
  type DecisionListOut,
  type EntryPause,
  type FlattenPreview,
  type GlossaryOut,
  type HealthOut,
  type IndicatorsOut,
  type InsightsOut,
  type LlmCallDetail,
  type LlmCallListOut,
  type MarketOut,
  type Needs,
  type NotificationSettings,
  type OutcomeScorecardOut,
  type OutcomeStatsOut,
  type PerformanceOut,
  type PositionsOut,
  type FillsPageOut,
  type PromptInfo,
  type ReadinessOut,
  type Settings,
  type SettingsHistoryPageOut,
  type SettingsMetaOut,
  type SettingsOut,
  type SettingsPreviewOut,
  type SettingsSaveIn,
  type SettingsSaveOut,
  type ShadowIn,
  type ShadowOut,
  type Snapshot,
  type SourceOut,
  type Tf,
  type TradeListOut,
  type UniverseOut,
  type UniverseSyncOut,
  type CheckDetailOut,
  type WhatIfJobOut,
  type WhatIfSpecIn,
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

/**
 * Newest-first activity log. `kind` filters server-side; pages follow the
 * `next_before` cursor so "Show more" keeps loading older entries.
 */
export function useActivity(kind?: string, limit = 25) {
  return useInfiniteQuery({
    queryKey: ["activity", kind ?? "all"],
    queryFn: ({ pageParam }) => {
      const qs = new URLSearchParams({ limit: String(limit) });
      if (kind) qs.set("kind", kind);
      if (pageParam) qs.set("before", String(pageParam));
      return api<ActivityListOut>(`/api/activity?${qs}`);
    },
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_before ?? undefined,
    staleTime: 15_000,
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

/** The Needs you list (items + health line); the snapshot carries it too. */
export function useNeeds() {
  return useQuery({
    queryKey: ["needs"],
    queryFn: () => api<Needs>("/api/needs"),
    staleTime: 15_000,
  });
}

/** Rolling quarter-hour slots, summary and grouped decisions (cycles card). */
export function useCyclesRecent(hours = 24) {
  return useQuery({
    queryKey: ["cycles-recent", hours],
    queryFn: () => api<CyclesRecent>(`/api/cycles/recent?hours=${hours}`),
    staleTime: 15_000,
  });
}

export function useMarkets() {
  return useQuery({
    queryKey: ["markets"],
    queryFn: () => api<MarketOut[]>("/api/markets"),
    staleTime: 60_000,
  });
}

/** The Universe screen payload (item 026): check line, tiles, calls and
 * every EUR market with its one status. Refetched every 60 s while open
 * and on every snapshot push. */
export function useUniverse() {
  return useQuery({
    queryKey: ["universe"],
    queryFn: () => api<UniverseOut>("/api/universe"),
    staleTime: 60_000,
    refetchInterval: 60_000,
  });
}

/** Latest + in-use check runs for the funnel card. */
export function useUniverseCheck() {
  return useQuery({
    queryKey: ["universe-check"],
    queryFn: () => api<CheckDetailOut>("/api/universe/check"),
    staleTime: 30_000,
  });
}

/** Latest run per sync job; polls every 2 s while a job runs. */
export function useUniverseSync() {
  return useQuery({
    queryKey: ["universe-sync"],
    queryFn: () => api<UniverseSyncOut>("/api/universe/sync"),
    staleTime: 5_000,
    refetchInterval: (query) =>
      query.state.data?.jobs.some((j) => j.status === "running") ? 2_000 : 15_000,
  });
}

/** POST /api/universe/sync/{job}/retry — reruns a failed job's failures. */
export function useRetrySyncJob() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (job: string) =>
      api<{ job: string; status: string }>(
        `/api/universe/sync/${encodeURIComponent(job)}/retry`,
        { method: "POST" },
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["universe-sync"] });
      void client.invalidateQueries({ queryKey: ["universe"] });
      void client.invalidateQueries({ queryKey: ["universe-check"] });
    },
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

export function useGlossary() {
  return useQuery({
    queryKey: ["glossary"],
    queryFn: () => api<GlossaryOut>("/api/glossary"),
    staleTime: 300_000,
  });
}

export function useReadiness() {
  return useQuery({
    queryKey: ["readiness"],
    queryFn: () => api<ReadinessOut>("/api/readiness"),
    staleTime: 60_000,
  });
}

export function useBriefings(type?: "daily" | "weekly", limit = 30) {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (type) qs.set("type", type);
  return useQuery({
    queryKey: ["briefings", type ?? "all", limit],
    queryFn: () => api<BriefingListOut>(`/api/briefings?${qs}`),
    staleTime: 60_000,
  });
}

/** Pause new entries on a pair ("*" pauses all coins). `until` ISO or null. */
export function usePauseEntries() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ pair, until }: { pair: string; until: string | null }) =>
      api<EntryPause>(
        pair === "*"
          ? "/api/agent/entry-pause"
          : `/api/markets/${encodeURIComponent(pair)}/pause`,
        { method: "PUT", body: JSON.stringify({ until }) },
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["snapshot"] });
      void client.invalidateQueries({ queryKey: ["markets"] });
      void client.invalidateQueries({ queryKey: ["universe"] });
    },
  });
}

export function useResumeEntries() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (pair: string) =>
      api<{ ok: boolean }>(
        pair === "*"
          ? "/api/agent/entry-pause"
          : `/api/markets/${encodeURIComponent(pair)}/pause`,
        { method: "DELETE" },
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["snapshot"] });
      void client.invalidateQueries({ queryKey: ["markets"] });
      void client.invalidateQueries({ queryKey: ["universe"] });
    },
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
    mutationFn: (body: SettingsSaveIn) =>
      api<SettingsSaveOut>("/api/settings", {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    onSuccess: (data) => {
      client.setQueryData(["settings"], data.settings);
      void client.invalidateQueries({ queryKey: ["settings-meta"] });
      void client.invalidateQueries({ queryKey: ["settings-history"] });
    },
  });
}

export function useSettingsMeta() {
  return useQuery({
    queryKey: ["settings-meta"],
    queryFn: () => api<SettingsMetaOut>("/api/settings/meta"),
    staleTime: 60_000,
  });
}

/** POST /api/settings/preview: money, tags and errors; saves nothing. */
export function useSettingsPreview() {
  return useMutation({
    mutationFn: (settings: Settings) =>
      api<SettingsPreviewOut>("/api/settings/preview", {
        method: "POST",
        body: JSON.stringify(settings),
      }),
  });
}

/** Versioned settings history, newest first; "Show earlier" pages on. */
export function useSettingsHistory() {
  return useInfiniteQuery({
    queryKey: ["settings-history"],
    queryFn: ({ pageParam }) => {
      const qs = new URLSearchParams();
      if (pageParam) qs.set("cursor", String(pageParam));
      const suffix = qs.size ? `?${qs}` : "";
      return api<SettingsHistoryPageOut>(`/api/settings/history${suffix}`);
    },
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** PUT /api/notifications: the SMTP block only, saved as a version. */
export function useSaveSmtpSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { smtp: NotificationSettings["smtp"]; confirm: boolean }) =>
      api<NotificationSettings>("/api/notifications", {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["notifications"] });
      void client.invalidateQueries({ queryKey: ["settings"] });
      void client.invalidateQueries({ queryKey: ["settings-meta"] });
      void client.invalidateQueries({ queryKey: ["settings-history"] });
    },
  });
}

export function useUpdateMarket() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      market,
      active,
      confirm = false,
      confirmLooser = false,
    }: {
      market: string;
      active: boolean;
      confirm?: boolean;
      confirmLooser?: boolean;
    }) =>
      api<MarketOut>(`/api/markets/${encodeURIComponent(market)}`, {
        method: "PUT",
        body: JSON.stringify({ active, confirm, confirm_looser: confirmLooser }),
      }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["markets"] });
      client.invalidateQueries({ queryKey: ["universe"] });
      client.invalidateQueries({ queryKey: ["universe-sync"] });
    },
  });
}

export function useUpdateSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      enabled,
      confirm = false,
    }: {
      id: string;
      enabled: boolean;
      confirm?: boolean;
    }) =>
      api<SourceOut>(`/api/sources/${id}`, {
        method: "PUT",
        body: JSON.stringify({ enabled, confirm }),
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
          data?:
            | Snapshot
            | {
                title: string;
                body: string;
                event?: string;
                severity?: "critical" | "warning" | "info";
              };
        };
        if (message.type === "snapshot" && message.data) {
          setConnected(true);
          client.setQueryData(["snapshot"], message.data as Snapshot);
          // A push means backend state changed (cycle, fill, control action):
          // every other query goes stale so open screens refetch.
          void client.invalidateQueries({
            predicate: (query) => query.queryKey[0] !== "snapshot",
          });
        } else if (
          message.type === "alert" &&
          message.data &&
          "title" in message.data
        ) {
          pushToast({
            event: message.data.event ?? "alert",
            severity: message.data.severity ?? "info",
            title: message.data.title,
            body: message.data.body,
          });
          if (
            "Notification" in window &&
            Notification.permission === "granted"
          ) {
            new Notification(message.data.title, { body: message.data.body });
          }
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

/* --------------------------------------------------------------------------
 * Evidence (PR 8): insights, What-if, shadows, Ask and weekly reviews.
 * ------------------------------------------------------------------------ */

export function useInsights(params: {
  dimension?: string;
  from?: string;
  to?: string;
  decider?: string;
  prompt_version?: string;
}) {
  const qs = new URLSearchParams();
  if (params.dimension) qs.set("dimension", params.dimension);
  if (params.from) qs.set("from", params.from);
  if (params.to) qs.set("to", params.to);
  if (params.decider) qs.set("decider", params.decider);
  if (params.prompt_version) qs.set("prompt_version", params.prompt_version);
  return useQuery({
    queryKey: ["insights", params],
    queryFn: () => api<InsightsOut>(`/api/insights?${qs}`),
    staleTime: 60_000,
  });
}

export function useOutcomeStats(group?: string, from?: string, to?: string) {
  const qs = new URLSearchParams();
  if (group) qs.set("group", group);
  if (from) qs.set("from", from);
  if (to) qs.set("to", to);
  return useQuery({
    queryKey: ["outcome-stats", group ?? "", from ?? "", to ?? ""],
    queryFn: () => api<OutcomeStatsOut>(`/api/outcomes/stats?${qs}`),
    staleTime: 60_000,
  });
}

export function useWhatIfList() {
  return useQuery({
    queryKey: ["whatif"],
    queryFn: () => api<{ items: WhatIfJobOut[] }>("/api/whatif"),
    staleTime: 30_000,
  });
}

export function useWhatIf(id: string | null) {
  return useQuery({
    queryKey: ["whatif", id],
    queryFn: () => api<WhatIfJobOut>(`/api/whatif/${id}`),
    enabled: id !== null,
    refetchInterval: (q) =>
      q.state.data?.status === "queued" || q.state.data?.status === "running"
        ? 1500
        : false,
  });
}

export function useRunWhatIf() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (spec: WhatIfSpecIn) =>
      api<WhatIfJobOut>("/api/whatif", {
        method: "POST",
        body: JSON.stringify(spec),
      }),
    onSuccess: (job) => {
      client.setQueryData(["whatif", job.id], job);
      void client.invalidateQueries({ queryKey: ["whatif"], exact: true });
    },
  });
}

export function useSaveWhatIf() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name?: string }) =>
      api<WhatIfJobOut>(`/api/whatif/${id}/save`, {
        method: "POST",
        body: JSON.stringify({ name: name ?? null }),
      }),
    onSuccess: (job) => {
      client.setQueryData(["whatif", job.id], job);
      void client.invalidateQueries({ queryKey: ["whatif"], exact: true });
    },
  });
}

export function useApplyWhatIf() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api<{ ok: boolean; changes: string[] }>(`/api/whatif/${id}/apply`, {
        method: "POST",
        body: JSON.stringify({ confirm: "APPLY" }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["settings"] });
      void client.invalidateQueries({ queryKey: ["snapshot"] });
    },
  });
}

export function useShadows() {
  return useQuery({
    queryKey: ["shadows"],
    queryFn: () => api<{ items: ShadowOut[] }>("/api/shadows"),
    staleTime: 30_000,
  });
}

export function useCreateShadow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ShadowIn) =>
      api<ShadowOut>("/api/shadows", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["shadows"] });
    },
  });
}

export function useStopShadow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api<{ ok: boolean }>(`/api/shadows/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["shadows"] });
    },
  });
}

export function usePromoteShadow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api<{ ok: boolean }>(`/api/shadows/${id}/promote`, {
        method: "POST",
        body: JSON.stringify({ confirm: "PROMOTE" }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["shadows"] });
      void client.invalidateQueries({ queryKey: ["settings"] });
    },
  });
}

export function useAskConversations() {
  return useQuery({
    queryKey: ["ask-conversations"],
    queryFn: () => api<AskConversationListOut>("/api/ask/conversations"),
    staleTime: 30_000,
  });
}

export function useAskConversation(id: string | null) {
  return useQuery({
    queryKey: ["ask-conversation", id],
    queryFn: () => api<AskConversationOut>(`/api/ask/conversations/${id}`),
    enabled: id !== null,
  });
}

export function useAsk() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: AskIn) =>
      api<AskOut>("/api/ask", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["ask-conversations"] });
      void client.invalidateQueries({ queryKey: ["ask-conversation"] });
    },
  });
}

/* --------------------------------------------------------------------------
 * Positions & orders (016).
 * ------------------------------------------------------------------------ */

export function usePositions(account: string) {
  return useQuery({
    queryKey: ["positions", account],
    queryFn: () =>
      api<PositionsOut>(`/api/positions?account=${encodeURIComponent(account)}`),
    staleTime: 10_000,
  });
}

export type FillFilters = {
  pair?: string;
  side?: string;
  reason?: string;
  from?: string;
  to?: string;
};

/** Order history, newest first; pages follow the opaque `next_cursor`. */
export function useFills(filters: FillFilters) {
  return useInfiniteQuery({
    queryKey: ["fills", filters],
    queryFn: ({ pageParam }) => {
      const qs = new URLSearchParams();
      for (const [key, value] of Object.entries(filters)) {
        if (value) qs.set(key, value);
      }
      if (pageParam) qs.set("cursor", String(pageParam));
      return api<FillsPageOut>(`/api/fills?${qs}`);
    },
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    staleTime: 10_000,
  });
}

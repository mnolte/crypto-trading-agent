import type { components } from "./schema";

export type Snapshot = components["schemas"]["Snapshot"];
export type Settings = components["schemas"]["Settings-Input"];
export type SettingsOut = components["schemas"]["Settings-Output"];
export type SessionOut = components["schemas"]["SessionOut"];
export type HealthOut = components["schemas"]["app__api__routes__HealthOut"];
export type AgentStatus = Snapshot["status"];

export type Tf = "1h" | "4h" | "1d";
export type CandleFeed = components["schemas"]["CandleFeed"];
export type CandlePoint = components["schemas"]["CandlePoint"];
export type ChartMarker = components["schemas"]["ChartMarker"];
export type PositionLines = components["schemas"]["PositionLines"];
export type Decision = components["schemas"]["Decision"];
export type DecisionListOut = components["schemas"]["DecisionListOut"];
export type DecisionDetail = components["schemas"]["DecisionDetail"];
export type IndicatorsOut = components["schemas"]["IndicatorsOut"];
export type PerformanceOut = components["schemas"]["PerformanceOut"];
export type TradeOut = components["schemas"]["TradeOut"];
export type TradeListOut = components["schemas"]["TradeListOut"];
export type FlattenPreview = components["schemas"]["FlattenPreview"];
export type PromptInfo = components["schemas"]["PromptInfo"];
export type MarketOut = components["schemas"]["MarketOut"];
export type SourceOut = components["schemas"]["SourceOut"];
export type SecretTestOut = components["schemas"]["SecretTestOut"];
export type Regime = components["schemas"]["Regime"];
export type Plan = components["schemas"]["Plan"];
export type Position = components["schemas"]["Position"];
export type ActivityItem = components["schemas"]["ActivityItem"];
export type PriceInfo = components["schemas"]["PriceInfo"];
export type KillInfo = components["schemas"]["KillInfo"];
export type IndicatorRowOut = components["schemas"]["IndicatorRowOut"];
export type LevelOut = components["schemas"]["LevelOut"];
export type Signal = components["schemas"]["Signal"];
export type Gate = components["schemas"]["Gate"];
export type CheckOut = components["schemas"]["CheckOut"];
export type LlmCallListItem = components["schemas"]["LlmCallListItem"];
export type LlmCallListOut = components["schemas"]["LlmCallListOut"];
export type LlmCallDetail = components["schemas"]["LlmCallDetail"];
export type LlmCallAttemptOut = components["schemas"]["LlmCallAttemptOut"];
export type ReplayInfo = components["schemas"]["ReplayInfo"];
export type CostsOut = components["schemas"]["CostsOut"];
export type OutcomeOut = components["schemas"]["OutcomeOut"];
export type OutcomeScorecardOut = components["schemas"]["OutcomeScorecardOut"];
export type NotificationSettings = components["schemas"]["NotificationSettings"];
export type Usage = components["schemas"]["Usage"];
export type EntryPause = components["schemas"]["EntryPause"];
export type RiskBudget = components["schemas"]["RiskBudget"];
export type GlossaryOut = components["schemas"]["GlossaryOut"];
export type ReadinessOut = components["schemas"]["ReadinessOut"];
export type ReadinessRowOut = components["schemas"]["ReadinessRowOut"];
export type BriefingOut = components["schemas"]["BriefingOut"];
export type BriefingListOut = components["schemas"]["BriefingListOut"];
export type SettingsHistoryOut = components["schemas"]["SettingsHistoryOut"];
export type InsightsOut = components["schemas"]["InsightsOut"];
export type InsightSliceOut = components["schemas"]["InsightSliceOut"];
export type InsightFinding = components["schemas"]["InsightFinding"];
export type CalibrationBucketOut = components["schemas"]["CalibrationBucketOut"];
export type DeciderOption = components["schemas"]["DeciderOption"];
export type WhatIfJobOut = components["schemas"]["WhatIfJobOut"];
export type WhatIfResult = components["schemas"]["WhatIfResult"];
export type WhatIfMetric = components["schemas"]["WhatIfMetric"];
export type WhatIfTrade = components["schemas"]["WhatIfTrade"];
export type WhatIfSpecIn = components["schemas"]["WhatIfSpecIn"];
export type ShadowOut = components["schemas"]["ShadowOut"];
export type ShadowIn = components["schemas"]["ShadowIn"];
export type AskOut = components["schemas"]["AskOut"];
export type AskIn = components["schemas"]["AskIn"];
export type AskContext = components["schemas"]["AskContext"];
export type AskMessageOut = components["schemas"]["AskMessageOut"];
export type AskCitation = components["schemas"]["AskCitation"];
export type AskConversationListOut = components["schemas"]["AskConversationListOut"];
export type AskConversationOut = components["schemas"]["AskConversationOut"];
export type OutcomeStatsOut = components["schemas"]["OutcomeStatsOut"];
export type OutcomeStatsRow = components["schemas"]["OutcomeStatsRow"];

let localToken: string | null = null;

async function fetchLocalToken(): Promise<string> {
  const response = await fetch("/api/session");
  if (!response.ok) throw new Error(`GET /api/session failed: ${response.status}`);
  const body = (await response.json()) as SessionOut;
  return body.token;
}

/** The X-Local-Token for this session; the served UI reads it once at load. */
export async function getLocalToken(): Promise<string> {
  if (localToken === null) {
    localToken = await fetchLocalToken();
  }
  return localToken;
}

async function send(path: string, init: RequestInit | undefined, write: boolean): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (write) {
    headers.set("X-Local-Token", await getLocalToken());
  }
  if (init?.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(path, { ...init, headers });
}

/** Fetch wrapper. Every write (non-GET) carries the X-Local-Token header. */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const write = init?.method !== undefined && init.method !== "GET";
  let response = await send(path, init, write);
  if (write && response.status === 403) {
    // The token is minted at backend startup; a restart leaves ours stale.
    localToken = null;
    response = await send(path, init, write);
  }
  if (!response.ok) {
    let detail = "";
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = ` — ${body.detail}`;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`${init?.method ?? "GET"} ${path}: ${response.status}${detail}`);
  }
  return (await response.json()) as T;
}

export function post<T>(path: string): Promise<T> {
  return api<T>(path, { method: "POST" });
}

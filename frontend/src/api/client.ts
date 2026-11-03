import type { components } from "./schema";

// TODO: replace with schema types after regen (PR 14 backend additions).
interface SnapshotNewFields {
  needs?: Needs;
  btc_return_since_start_pct?: number | null;
  account_started_at?: string | null;
}
interface PositionNewFields {
  stop_dist_pct?: string | null;
  stop_dist_atr?: string | null;
  pnl_r?: string | null;
  stop_above_entry?: boolean;
  stop_near?: boolean;
}
interface MarketOutNewFields {
  price?: string | null;
  chg_24h_pct?: string | null;
  regime?: components["schemas"]["Regime"] | null;
  rules_state?: "passed" | "held" | "failed" | "paused" | "warming" | "none" | null;
  rules_detail?: string | null;
  last_action_action?: string | null;
  last_action_at?: string | null;
}
interface SettingsNewFields {
  ui?: { stop_near_atr: string | number };
}

export type Snapshot = components["schemas"]["Snapshot"] & SnapshotNewFields;
export type Settings = components["schemas"]["Settings-Input"] & SettingsNewFields;
export type SettingsOut = components["schemas"]["Settings-Output"] & SettingsNewFields;
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
export type MarketOut = components["schemas"]["MarketOut"] & MarketOutNewFields;
export type SourceOut = components["schemas"]["SourceOut"];
export type SecretTestOut = components["schemas"]["SecretTestOut"];
export type Regime = components["schemas"]["Regime"];
export type Plan = components["schemas"]["Plan"];
export type Position = components["schemas"]["Position"] & PositionNewFields;
export type ActivityItem = components["schemas"]["ActivityItem"];
export type ActivityOut = components["schemas"]["ActivityOut"];
export type ActivityListOut = components["schemas"]["ActivityListOut"];
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

// TODO: replace with schema types after regen (PR 14 backend additions).

export interface NeedsItem {
  kind: string;
  title: string;
  detail: string | null;
  action_label: string;
  action_target: string;
  since: string | null;
}

export interface HealthItem {
  key: string;
  label: string;
  state: "ok" | "warn" | "down" | "off" | "unknown";
  value: string | null;
}

export interface Needs {
  items: NeedsItem[];
  health: HealthItem[];
}

export type CycleSlotState =
  | "entry"
  | "exit_gain"
  | "exit_loss"
  | "stop_moved"
  | "error"
  | "no_change"
  | "missed"
  | "paused"
  | "next"
  | "future";

export interface CycleSlotChange {
  kind: "entry" | "exit" | "stop_move" | "error";
  pair: string | null;
  detail: string;
  decision_id: string | null;
  exit_gain: boolean | null;
}

export interface CycleSlot {
  at: string;
  state: CycleSlotState;
  missed_reason: string | null;
  cycle: number | null;
  changes: CycleSlotChange[];
  jobs: string[];
}

export interface CycleGroup {
  label: string;
  reason: string | null;
  pairs: string[];
  count: number;
  decision_id: string | null;
  detail: string | null;
  change: boolean;
}

export interface CycleDecisions {
  slot_at: string;
  cycle: number | null;
  groups: CycleGroup[];
  changes: CycleSlotChange[];
  decision_count: number;
  missed: boolean;
}

export interface MissedRange {
  from_at: string;
  to_at: string;
  reason: string | null;
  count: number;
}

export interface CyclesSummary {
  ran: number;
  scheduled: number;
  missed: MissedRange[];
  errors: number;
  cycle_id: number;
  running_since: string | null;
}

export interface CyclesRecent {
  slots: CycleSlot[];
  summary: CyclesSummary;
  decisions: CycleDecisions[];
}

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

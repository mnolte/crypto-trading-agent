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
export type SettingsHistoryPageOut = components["schemas"]["SettingsHistoryPageOut"];
export type SettingsHistoryVersionOut =
  components["schemas"]["SettingsHistoryVersionOut"];
export type SettingsHistoryRowOut = components["schemas"]["SettingsHistoryRowOut"];
export type SettingsMetaOut = components["schemas"]["SettingsMetaOut"];
export type SettingsMetaItem = components["schemas"]["SettingsMetaItem"];
export type SettingsPreviewOut = components["schemas"]["SettingsPreviewOut"];
export type SettingsSaveIn = components["schemas"]["SettingsSaveIn"];
export type SettingsSaveOut = components["schemas"]["SettingsSaveOut"];
export type SettingsChangeOut = components["schemas"]["SettingsChangeOut"];
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

export type NeedsItem = components["schemas"]["NeedsItem"];
export type HealthItem = components["schemas"]["HealthItem"];
export type Needs = components["schemas"]["Needs"];
export type CycleSlot = components["schemas"]["CycleSlot"];
export type CycleSlotState = CycleSlot["state"];
export type CycleSlotChange = components["schemas"]["CycleSlotChange"];
export type CycleGroup = components["schemas"]["CycleGroup"];
export type CycleDecisions = components["schemas"]["CycleDecisions"];
export type MissedRange = components["schemas"]["MissedRange"];
export type CyclesSummary = components["schemas"]["CyclesSummary"];
export type CyclesRecent = components["schemas"]["CyclesRecent"];

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

/** A failed API call in the stable error shape — code, message and,
 * on 500s, the error id that matches its line on the Logs screen. */
export class ApiError extends Error {
  status: number;
  code: string;
  errorId?: string;
  requestId?: string;
  retryAfter?: string;
  /** Looser-change details on a 409 looser_needs_confirm. */
  changes?: { key: string; old?: unknown; new?: unknown; direction?: string }[];

  constructor(opts: {
    status: number;
    code: string;
    message: string;
    errorId?: string;
    requestId?: string;
    retryAfter?: string;
    changes?: { key: string; old?: unknown; new?: unknown; direction?: string }[];
  }) {
    super(opts.message);
    this.name = "ApiError";
    this.status = opts.status;
    this.code = opts.code;
    this.errorId = opts.errorId;
    this.requestId = opts.requestId;
    this.retryAfter = opts.retryAfter;
    this.changes = opts.changes;
  }
}

const API_TIMEOUT_MS = 15_000;

async function send(path: string, init: RequestInit | undefined, write: boolean): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (write) {
    headers.set("X-Local-Token", await getLocalToken());
  }
  if (init?.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  // A hung request fails after 15 s unless the caller sets its own signal.
  const signal = init?.signal ?? AbortSignal.timeout(API_TIMEOUT_MS);
  return fetch(path, { ...init, headers, signal });
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
    const requestId = response.headers.get("x-request-id") ?? undefined;
    const retryAfter = response.headers.get("retry-after") ?? undefined;
    let body: {
      error?: {
        code?: string;
        message?: string;
        error_id?: string;
        changes?: { key: string; old?: unknown; new?: unknown; direction?: string }[];
      };
      detail?: string;
    } = {};
    try {
      body = (await response.json()) as typeof body;
    } catch {
      /* non-JSON error body */
    }
    const err = body.error;
    throw new ApiError({
      status: response.status,
      code: err?.code ?? `http_${response.status}`,
      message:
        err?.message ??
        body.detail ??
        `${init?.method ?? "GET"} ${path}: ${response.status}`,
      errorId: err?.error_id,
      requestId,
      retryAfter,
      changes: err?.changes,
    });
  }
  return (await response.json()) as T;
}

export function post<T>(path: string): Promise<T> {
  return api<T>(path, { method: "POST" });
}

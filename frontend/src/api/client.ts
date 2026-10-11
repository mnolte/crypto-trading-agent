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

let localToken: string | null = null;

/** The X-Local-Token for this session; the served UI reads it once at load. */
export async function getLocalToken(): Promise<string> {
  if (localToken === null) {
    const response = await fetch("/api/session");
    if (!response.ok) throw new Error(`GET /api/session failed: ${response.status}`);
    const body = (await response.json()) as SessionOut;
    localToken = body.token;
  }
  return localToken;
}

/** Fetch wrapper. Every write (non-GET) carries the X-Local-Token header. */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.method && init.method !== "GET") {
    headers.set("X-Local-Token", await getLocalToken());
  }
  if (init?.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
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

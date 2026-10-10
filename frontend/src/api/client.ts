import type { components } from "./schema";

export type Snapshot = components["schemas"]["Snapshot"];
export type Settings = components["schemas"]["Settings-Input"];
export type SettingsOut = components["schemas"]["Settings-Output"];
export type SessionOut = components["schemas"]["SessionOut"];
export type HealthOut = components["schemas"]["HealthOut"];
export type AgentStatus = Snapshot["status"];

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
  if (!response.ok) throw new Error(`${init?.method ?? "GET"} ${path}: ${response.status}`);
  return (await response.json()) as T;
}

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { api, type HealthOut, type SettingsOut, type Snapshot } from "./client";

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

/**
 * Subscribe to the snapshot WebSocket. The server pushes {"type":"snapshot"}
 * after every change; on reconnect (with backoff) we refetch the snapshot.
 */
export function useSnapshotStream() {
  const client = useQueryClient();
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
        void client.invalidateQueries({ queryKey: ["snapshot"] });
      };
      ws.onmessage = (event) => {
        const message = JSON.parse(event.data as string) as {
          type: string;
          data?: Snapshot;
        };
        if (message.type === "snapshot" && message.data) {
          client.setQueryData(["snapshot"], message.data);
        }
      };
      ws.onclose = () => {
        if (closed) return;
        attempt += 1;
        const delay = Math.min(30_000, 500 * 2 ** attempt);
        timer = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [client]);
}

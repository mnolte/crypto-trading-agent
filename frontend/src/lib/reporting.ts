/** Client error reporting: window errors, unhandled rejections and
 * console errors/warnings go to POST /api/logs/client — at most ten a
 * minute, and a failed report is dropped silently (it must never loop).
 * The returned client error id is searchable on the Logs screen. */

import { getLocalToken } from "../api/client";

const MAX_PER_MINUTE = 10;
const sent: number[] = [];
let installed = false;
let reporting = false;

export function reportClient(
  level: "warning" | "error",
  message: string,
  data?: Record<string, unknown>,
): string {
  const id = "e-" + crypto.randomUUID().replace(/-/g, "").slice(0, 8);
  const now = Date.now();
  while (sent.length > 0 && now - sent[0] > 60_000) sent.shift();
  if (sent.length >= MAX_PER_MINUTE) return id;
  sent.push(now);
  void getLocalToken()
    .then((token) =>
      fetch("/api/logs/client", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Local-Token": token,
        },
        body: JSON.stringify({
          level,
          message: message.slice(0, 2000),
          data: { ...data, client_error_id: id, route: location.pathname },
        }),
      }),
    )
    .catch(() => {
      /* a failed report is dropped — never log a failure about logging */
    });
  return id;
}

export function installReporting(): void {
  if (installed) return;
  installed = true;
  window.addEventListener("error", (event) =>
    reportClient("error", event.message || "window error", {
      source: event.filename,
      line: event.lineno,
    }),
  );
  window.addEventListener("unhandledrejection", (event) =>
    reportClient(
      "error",
      event.reason instanceof Error ? event.reason.message : String(event.reason),
      { rejection: true },
    ),
  );
  for (const kind of ["error", "warn"] as const) {
    const original = console[kind];
    console[kind] = (...args: unknown[]) => {
      original.apply(console, args);
      if (reporting) return;
      reporting = true;
      try {
        reportClient(
          kind === "warn" ? "warning" : "error",
          args.map((a) => String(a)).join(" "),
          { via: `console.${kind}` },
        );
      } finally {
        reporting = false;
      }
    };
  }
}

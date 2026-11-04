import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import type { components } from "../api/schema";
import { api, ApiError } from "../api/client";
import { AppShell } from "../components/AppShell";

type LogLine = components["schemas"]["LogLineOut"];
type LogsList = components["schemas"]["LogsListOut"];
type LogProblem = components["schemas"]["LogProblemOut"];
type Problems = components["schemas"]["ProblemsOut"];
type LogSummary = components["schemas"]["LogSummaryOut"];
type Around = components["schemas"]["AroundOut"];

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;
const PERIODS = [
  { id: "24h", label: "24 h", hours: 24 },
  { id: "7d", label: "7 days", hours: 24 * 7 },
  { id: "30d", label: "30 days", hours: 24 * 30 },
  { id: "all", label: "all", hours: null },
] as const;

const COMPONENTS = [
  "agent.scheduler",
  "agent.rules",
  "agent.decider",
  "agent.risk",
  "agent.watcher",
  "broker.paper",
  "market.bitvavo",
  "market.stream",
  "market.universe",
  "context.collector",
  "notify",
  "ops.backup",
  "ops.jobs",
  "ops.logs",
  "app",
  "api",
  "frontend",
  "python.warning",
];

function useLogsQueryString() {
  const [params] = useSearchParams();
  return useMemo(() => {
    const levels = (params.get("level") ?? "WARNING,ERROR,CRITICAL")
      .split(",")
      .filter(Boolean);
    return {
      levels,
      period: params.get("period") ?? "24h",
      q: params.get("q") ?? "",
      component: params.get("component") ?? "",
      pair: params.get("pair") ?? "",
      cycleId: params.get("cycle_id") ?? "",
      errorId: params.get("error_id") ?? "",
      requestId: params.get("request_id") ?? "",
    };
  }, [params]);
}

function buildQuery(f: ReturnType<typeof useLogsQueryString>, after?: string) {
  const q = new URLSearchParams();
  q.set("level", f.levels.join(","));
  if (f.q) q.set("q", f.q);
  if (f.component) q.set("component", f.component);
  if (f.pair) q.set("pair", f.pair);
  if (f.cycleId) q.set("cycle_id", f.cycleId);
  if (f.errorId) q.set("error_id", f.errorId);
  if (f.requestId) q.set("request_id", f.requestId);
  const period = PERIODS.find((p) => p.id === f.period);
  if (period?.hours != null && !f.cycleId) {
    q.set("since", new Date(Date.now() - period.hours * 3600_000).toISOString());
  }
  if (after) q.set("after", after);
  q.set("limit", "200");
  return q.toString();
}

function fmtBytes(n: number): string {
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(0)} KB`;
  return `${n} B`;
}

function fmtTime(ts: string | null | undefined): string {
  if (!ts) return "—";
  return ts.slice(11, 19);
}

function fmtDay(ts: string | null | undefined): string {
  if (!ts) return "";
  return ts.slice(5, 10);
}

const LEVEL_CLASS: Record<string, string> = {
  DEBUG: "lv lv--debug",
  INFO: "lv lv--info",
  WARNING: "lv lv--warn",
  ERROR: "lv lv--error",
  CRITICAL: "lv lv--crit",
};

export function Logs() {
  const filters = useLogsQueryString();
  const [params, setParams] = useSearchParams();
  const set = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };
  const queryClient = useQueryClient();

  const [older, setOlder] = useState<LogLine[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [follow, setFollow] = useState(false);

  const queryKey = ["logs", filters] as const;
  const { data } = useQuery({
    queryKey,
    queryFn: () => api<LogsList>(`/api/logs?${buildQuery(filters)}`),
    refetchInterval: 15_000,
  });
  useEffect(() => {
    setOlder([]);
    setNextCursor(data?.next ?? null);
  }, [data]);
  const lines = useMemo(() => [...(data?.lines ?? []), ...older], [data, older]);

  const { data: summary } = useQuery({
    queryKey: ["logs-summary"],
    queryFn: () => api<LogSummary>("/api/logs/summary"),
    refetchInterval: 15_000,
  });
  const { data: problems } = useQuery({
    queryKey: ["logs-problems"],
    queryFn: () => api<Problems>("/api/logs/problems"),
    refetchInterval: 30_000,
  });

  const loadOlder = useCallback(async () => {
    if (!nextCursor) return;
    const page = await api<LogsList>(`/api/logs?${buildQuery(filters, nextCursor)}`);
    setOlder((o) => [...o, ...page.lines]);
    setNextCursor(page.next ?? null);
  }, [filters, nextCursor]);

  // Follow new lines: the WS "logs" topic pushes WARNING+ live.
  const followRef = useRef(follow);
  followRef.current = follow;
  useEffect(() => {
    if (!follow) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data as string) as { type: string };
        if (msg.type === "logs" && followRef.current) {
          void queryClient.invalidateQueries({ queryKey: ["logs"] });
        }
      } catch {
        /* malformed frame */
      }
    };
    return () => ws.close();
  }, [follow, queryClient]);

  const toggleLevel = (lv: string) => {
    const next = filters.levels.includes(lv)
      ? filters.levels.filter((l) => l !== lv)
      : [...filters.levels, lv];
    set("level", next.length ? next.join(",") : "WARNING,ERROR,CRITICAL");
  };

  const unreviewed = (problems?.problems ?? []).filter(
    (p) => p.state === "unreviewed",
  );

  return (
    <AppShell
      title="Logs"
      sub="Everything the app logged · warnings and errors first"
    >
      <section className="logs-toolbar">
        <div className="logs-toolbar__levels">
          <span className="logs-toolbar__label">
            Levels · warning and up by default
          </span>
          {LEVELS.map((lv) => (
            <button
              key={lv}
              type="button"
              className={`logs-level${filters.levels.includes(lv) ? " logs-level--on" : ""}`}
              aria-pressed={filters.levels.includes(lv)}
              onClick={() => toggleLevel(lv)}
            >
              {lv}
              {summary?.counts_24h?.[lv] != null
                ? ` ${summary.counts_24h[lv]}`
                : ""}
            </button>
          ))}
        </div>
        <select
          aria-label="Period"
          value={filters.period}
          onChange={(e) => set("period", e.target.value)}
        >
          {PERIODS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
        <input
          type="search"
          placeholder="Search messages"
          value={filters.q}
          onChange={(e) => set("q", e.target.value)}
          aria-label="Search messages"
        />
        <select
          aria-label="Component"
          value={filters.component}
          onChange={(e) => set("component", e.target.value)}
        >
          <option value="">Any component</option>
          {COMPONENTS.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="Pair"
          value={filters.pair}
          onChange={(e) => set("pair", e.target.value)}
          aria-label="Pair"
          className="logs-toolbar__pair"
        />
        <input
          type="text"
          placeholder="Cycle c-…"
          value={filters.cycleId}
          onChange={(e) => set("cycle_id", e.target.value)}
          aria-label="Cycle id"
          className="logs-toolbar__cycle"
        />
        <a
          className="logs-toolbar__dl"
          href={`/api/logs/export.jsonl`}
          download
        >
          Download .jsonl
        </a>
        <label className="logs-follow">
          <input
            type="checkbox"
            checked={follow}
            onChange={(e) => setFollow(e.target.checked)}
          />
          Follow new lines
        </label>
      </section>

      {filters.cycleId && (
        <div className="logs-trace">
          Every line of cycle {filters.cycleId} · all levels
          <button type="button" onClick={() => set("cycle_id", "")}>
            Back to warnings and errors
          </button>
        </div>
      )}

      {!filters.cycleId && unreviewed.length + (problems?.problems.length ?? 0) > 0 && (
        <section className="card logs-problems">
          <h2>
            Recurring problems{" "}
            <span className="logs-problems__sub">
              warning and up · {unreviewed.length} unreviewed
            </span>
          </h2>
          <table>
            <thead>
              <tr>
                <th>Level</th>
                <th>Problem</th>
                <th>Count</th>
                <th>First · last seen</th>
                <th>Review</th>
              </tr>
            </thead>
            <tbody>
              {(problems?.problems ?? []).map((p) => (
                <ProblemRow key={p.fingerprint} problem={p} />
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="card logs-lines">
        <table>
          <thead>
            <tr>
              <th>Day</th>
              <th>Time</th>
              <th>Level</th>
              <th>Message</th>
              <th>Component</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line) => (
              <LogLineRow
                key={line.id}
                line={line}
                open={openId === line.id}
                onToggle={() =>
                  setOpenId(openId === line.id ? null : line.id)
                }
                onTrace={(cycleId) => set("cycle_id", cycleId)}
              />
            ))}
            {lines.length === 0 && (
              <tr>
                <td colSpan={5} className="logs-empty">
                  Nothing logged here yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {nextCursor && (
          <button type="button" className="logs-more" onClick={() => void loadOlder()}>
            Load older
          </button>
        )}
      </section>

      <footer className="logs-footer">
        logs.db {summary ? fmtBytes(summary.db_bytes) : "…"} · kept: debug
        7 d, info 30 d, warnings 180 d, errors 365 d
        {summary?.broken ? " · not writable — fallback file" : ""}
      </footer>
    </AppShell>
  );
}

function ProblemRow({ problem: p }: { problem: LogProblem }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [open, setOpen] = useState(false);
  const review = async (state: string) => {
    const token = await import("../api/client").then((m) => m.getLocalToken());
    const res = await fetch(
      `/api/logs/problems/${encodeURIComponent(p.fingerprint)}/review`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Local-Token": token,
        },
        body: JSON.stringify({ state, note }),
      },
    );
    if (!res.ok) {
      const err = (await res.json().catch(() => ({}))) as { error?: { message?: string } };
      throw new ApiError({
        status: res.status,
        code: err.error?.message ?? "review failed",
        message: err.error?.message ?? "review failed",
      });
    }
    setOpen(false);
    void queryClient.invalidateQueries({ queryKey: ["logs-problems"] });
  };
  return (
    <tr>
      <td>
        <span className={LEVEL_CLASS[p.level ?? "WARNING"]}>{p.level}</span>
      </td>
      <td className="logs-problems__msg">
        {p.example}
        {p.what_to_do && <span className="logs-problems__todo"> · {p.what_to_do}</span>}
      </td>
      <td>×{p.count}</td>
      <td>
        {fmtDay(p.first_seen)} {fmtTime(p.first_seen)} · {fmtDay(p.last_seen)}{" "}
        {fmtTime(p.last_seen)}
      </td>
      <td>
        {p.state !== "unreviewed" ? (
          <span>{p.state}</span>
        ) : open ? (
          <span className="logs-review">
            <input
              type="text"
              placeholder="a note is required"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <button type="button" disabled={!note.trim()} onClick={() => void review("expected")}>
              Expected
            </button>
            <button type="button" disabled={!note.trim()} onClick={() => void review("fixed")}>
              Fixed
            </button>
          </span>
        ) : (
          <button type="button" onClick={() => setOpen(true)}>
            Review
          </button>
        )}
      </td>
    </tr>
  );
}

function LogLineRow({
  line,
  open,
  onToggle,
  onTrace,
}: {
  line: LogLine;
  open: boolean;
  onToggle: () => void;
  onTrace: (cycleId: string) => void;
}) {
  const { data: around } = useQuery({
    queryKey: ["logs-around", line.id],
    queryFn: () => api<Around>(`/api/logs/${line.id}/around`),
    enabled: open,
  });
  const meta = [
    line.error_id ? `Error id ${line.error_id}` : null,
    line.request_id ? `Request ${line.request_id}` : null,
    line.process ? `process ${line.process}` : null,
    line.pid ? `pid ${line.pid}` : null,
    line.task ? `task ${line.task}` : null,
    line.job ? `job ${line.job}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <>
      <tr className="logs-line" onClick={onToggle}>
        <td className="logs-line__day">{fmtDay(line.ts)}</td>
        <td className="logs-line__t">{fmtTime(line.ts)}</td>
        <td>
          <span className={LEVEL_CLASS[line.level ?? "INFO"]}>{line.level}</span>
        </td>
        <td className="logs-line__msg">{line.message}</td>
        <td className="logs-line__logger">{line.logger}</td>
      </tr>
      {open && (
        <tr className="logs-line__open">
          <td colSpan={5}>
            <div className="logs-open">
              {meta && <p className="logs-open__meta">{meta}</p>}
              <div className="logs-open__actions">
                <button
                  type="button"
                  onClick={() =>
                    void navigator.clipboard.writeText(
                      `${line.ts} ${line.level} ${line.logger} ${line.message}`,
                    )
                  }
                >
                  Copy line
                </button>
                {line.error_id && (
                  <button
                    type="button"
                    onClick={() =>
                      void navigator.clipboard.writeText(line.error_id ?? "")
                    }
                  >
                    Copy error id
                  </button>
                )}
                {line.cycle_id && (
                  <button type="button" onClick={() => onTrace(line.cycle_id ?? "")}>
                    Every line of cycle {line.cycle_id}
                  </button>
                )}
              </div>
              {line.meaning && (
                <div className="logs-open__block">
                  <h3>What this means · {line.event}</h3>
                  <p>{line.meaning}</p>
                </div>
              )}
              {line.what_to_do && (
                <div className="logs-open__block">
                  <h3>What to do</h3>
                  <p>{line.what_to_do}</p>
                </div>
              )}
              {line.traceback && (
                <div className="logs-open__block">
                  <h3>Exception</h3>
                  <pre>{line.traceback}</pre>
                </div>
              )}
              {line.data && (
                <div className="logs-open__block">
                  <h3>Stored line</h3>
                  <pre>{line.data}</pre>
                </div>
              )}
              {around && (
                <div className="logs-open__block">
                  <h3>Surrounding lines · same process, all levels</h3>
                  <table>
                    <tbody>
                      {around.lines.map((a) => (
                        <tr
                          key={a.id}
                          className={a.id === line.id ? "logs-around--focus" : ""}
                        >
                          <td className="logs-line__t">{fmtTime(a.ts)}</td>
                          <td>
                            <span className={LEVEL_CLASS[a.level ?? "INFO"]}>
                              {a.level}
                            </span>
                          </td>
                          <td className="logs-line__logger">{a.logger}</td>
                          <td className="logs-line__msg">{a.message}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

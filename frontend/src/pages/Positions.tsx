import { useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { AppShell } from "../components/AppShell";
import { Card } from "../components/Card";
import { DecisionDrawer } from "../components/DecisionDrawer";
import { CardFetch } from "../lib/cardState";
import {
  formatDayShort,
  formatEur,
  formatPct,
  formatPrice,
  formatQty,
  formatSigned,
  formatSignedEur,
  formatTime,
  formatTimeUtc,
} from "../lib/format";
import {
  useFills,
  useMarkets,
  useNow,
  usePositions,
  useSettings,
  useSnapshot,
  type FillFilters,
} from "../api/hooks";
import type {
  FillOut,
  PaperOrderOut,
  PositionCardOut,
  ShadowRefOut,
  TodayItemOut,
} from "../api/client";

const DISPLAY_TZ = "Europe/Amsterdam";

const REASON_LABELS: Record<string, string> = {
  decision: "decision",
  stop: "stop",
  target: "target",
  time_stop: "time stop",
  model_sell: "model exit",
  flatten: "Stop & flatten",
  manual: "closed by you",
  kill: "kill switch",
};

const REASON_OPTIONS = Object.entries(REASON_LABELS);

function reasonLabel(code: string): string {
  return REASON_LABELS[code] ?? code;
}

/** "13 Oct 12:30:41" in Amsterdam; the UTC time goes on the title. */
function formatFillTime(iso: string): { text: string; utc: string } {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return { text: "—", utc: "" };
  return {
    text: d.toLocaleString("en-GB", {
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZone: DISPLAY_TZ,
    }),
    utc: `${formatTimeUtc(iso)} UTC`,
  };
}

/** "Tue 12:30" — the compact day+time used in stop history and decisions. */
function formatDayTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-GB", {
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: DISPLAY_TZ,
  });
}

/** "Sun 18 Oct 12:30" — the time-stop label. */
function formatDayMonthTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: DISPLAY_TZ,
  });
}

/** "opened today 12:30" / "opened Sun 16:45". */
function formatOpened(iso: string, now: number): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const today = new Date(now).toLocaleDateString("en-CA", { timeZone: DISPLAY_TZ });
  const day = d.toLocaleDateString("en-CA", { timeZone: DISPLAY_TZ });
  const time = formatTime(iso);
  return day === today ? `opened today ${time}` : `opened ${formatDayTime(iso)}`;
}

/** "4 min" / "23 h" / "1 d 20 h" — the held-time text. */
function formatHeld(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))} min`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h < 24) return m ? `${h} h ${m} min` : `${h} h`;
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh ? `${d} d ${rh} h` : `${d} d`;
}

/** Order-history date range for the `period`/`from`/`to` URL filters;
 * relative periods anchor at the moment the filters last changed. */
function orderRange(period: string, customFrom: string, customTo: string) {
  const nowMs = Date.now();
  if (period === "7d") {
    return { fromIso: new Date(nowMs - 7 * 864e5).toISOString(), toIso: "" };
  }
  if (period === "30d") {
    return { fromIso: new Date(nowMs - 30 * 864e5).toISOString(), toIso: "" };
  }
  if (period === "custom") {
    const to = customTo ? new Date(`${customTo}T23:59:59`).toISOString() : "";
    const from = customFrom ? new Date(`${customFrom}T00:00:00`).toISOString() : "";
    return { fromIso: from, toIso: to };
  }
  return { fromIso: "", toIso: "" };
}

function signedR(value: string | null | undefined): string {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(2)}R`;
}

function signedAtr(value: string | null | undefined): string {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(1)} ATR (1h)`;
}

export function Positions() {
  const snapshotQuery = useSnapshot();
  const { data: snapshot } = snapshotQuery;
  const { data: settings } = useSettings();
  const { data: markets } = useMarkets();
  const now = useNow(30_000);
  const [searchParams, setSearchParams] = useSearchParams();
  const [view, setView] = useState("live");
  const [decisionId, setDecisionId] = useState<string | null>(null);

  // Order-history filters live in the URL.
  const pair = searchParams.get("pair") ?? "";
  const side = searchParams.get("side") ?? "";
  const reason = searchParams.get("reason") ?? "";
  const period = searchParams.get("period") ?? "30d";
  const customFrom = searchParams.get("from") ?? "";
  const customTo = searchParams.get("to") ?? "";

  const { fromIso, toIso } = useMemo(
    () => orderRange(period, customFrom, customTo),
    [period, customFrom, customTo],
  );

  const filters: FillFilters = {
    ...(pair ? { pair } : {}),
    ...(side ? { side } : {}),
    ...(reason ? { reason } : {}),
    ...(fromIso ? { from: fromIso } : {}),
    ...(toIso ? { to: toIso } : {}),
  };

  const positionsQuery = usePositions(view);
  const data = positionsQuery.data;
  const fillsQuery = useFills(filters);
  const fills = fillsQuery.data?.pages.flatMap((p) => p.items) ?? [];
  const fillsTotal = fillsQuery.data?.pages[0]?.total ?? 0;

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next, { replace: true });
  };

  const clearFilters = () => {
    const next = new URLSearchParams(searchParams);
    for (const key of ["pair", "side", "reason", "period", "from", "to"]) next.delete(key);
    setSearchParams(next, { replace: true });
  };

  const exportHref = `/api/fills/export.csv?${new URLSearchParams(
    Object.fromEntries(Object.entries(filters).filter(([, v]) => v)),
  ).toString()}`;

  const pairs = useMemo(() => {
    const set = new Set<string>();
    for (const p of snapshot?.positions ?? []) set.add(p.pair);
    for (const m of markets ?? []) if (m.active) set.add(m.market);
    return [...set].sort();
  }, [snapshot?.positions, markets]);

  const isLive = view === "live";
  const selectedShadow = data?.shadows.find((s) => s.id === view) ?? null;
  const entryOrders = isLive
    ? (settings?.paper.entry_orders ?? "market")
    : (selectedShadow?.entry_orders ?? "market");
  const nearAtr = Number(settings?.ui.stop_near_atr ?? 1.5);

  const openDecision = (id: string | null | undefined) => {
    if (id) setDecisionId(id);
  };

  const emptyPositions = (() => {
    if (isLive) {
      const next = snapshot?.next_run_at;
      return `No open positions. The next cycle is at ${next ? formatTime(next) : "—"}.`;
    }
    return `Shadow ${selectedShadow?.name ?? view} holds nothing right now.`;
  })();

  const ordersEmpty = isLive
    ? entryOrders === "market"
      ? "None. Live entries are market orders (Settings → Paper broker → Entry orders). Limit entries waiting for a fill show here."
      : "None waiting."
    : entryOrders === "market"
      ? `None. ${selectedShadow?.name ?? "This shadow"} enters with market orders.`
      : "None waiting.";

  const closestText = (() => {
    const t = data?.totals;
    if (!t) return "";
    if (isLive) {
      if (!t.closest_pair || t.closest_atr == null) return "";
      const card = data?.positions.find((p) => p.pair === t.closest_pair);
      const amber = card?.stop_near
        ? ` · amber under ${formatQty(String(nearAtr))} ATR`
        : "";
      return `Closest to its stop: ${t.closest_pair} ${formatQty(String(t.closest_atr))} ATR${amber}`;
    }
    if (!selectedShadow) return "";
    return `${selectedShadow.name} · ${selectedShadow.model} · ${selectedShadow.prompt_version} · its own paper account since ${formatDayShort(selectedShadow.started_at)}`;
  })();

  return (
    <AppShell title="Positions & orders" sub="What the paper broker holds and did · read only">
      <section aria-label="Open positions" className="posscreen__section">
        <div className="posscreen__sectionhead">
          <h2 className="posscreen__h2">
            Open positions · {data?.totals.count ?? 0}
          </h2>
          <div className="posscreen__switch" role="group" aria-label="Whose positions">
            <button
              type="button"
              aria-pressed={isLive}
              className={isLive ? "posscreen__view posscreen__view--on" : "posscreen__view"}
              onClick={() => setView("live")}
            >
              Live
            </button>
            {(data?.shadows ?? []).map((s: ShadowRefOut) => (
              <button
                key={s.id}
                type="button"
                aria-pressed={view === s.id}
                className={view === s.id ? "posscreen__view posscreen__view--on" : "posscreen__view"}
                onClick={() => setView(s.id)}
              >
                {s.name}
              </button>
            ))}
          </div>
        </div>

        <Card className="posscreen__totals" label="Position totals">
          <CardFetch query={positionsQuery} name="positions" emptyWhen={false}>
            {data && (
              <>
                <div className="posscreen__totalsrow">
                  <Total label="Unrealised" value={formatSignedEur(data.totals.unrealised)} tone={Number(data.totals.unrealised) >= 0 ? "up" : "down"} />
                  <Total label="If every stop hits" value={formatSignedEur(data.totals.stop_hit_total)} tone={Number(data.totals.stop_hit_total) >= 0 ? "up" : "down"} />
                  <Total label="Open risk" value={`${formatQty(String(data.totals.open_risk_pct))}% of ${formatQty(String(data.totals.open_risk_cap_pct))}%`} />
                  <Total label="Exposure" value={`${formatEur(data.totals.exposure)} · ${Math.round(data.totals.exposure_pct)}%`} />
                  <Total label="Cash" value={formatEur(data.totals.cash)} />
                  <Total label="Fees" value={`${formatEur(data.totals.fees_today)} today · ${formatEur(data.totals.fees_total)} since start`} />
                </div>
                {closestText && <div className="posscreen__closest">{closestText}</div>}
              </>
            )}
          </CardFetch>
        </Card>

        <CardFetch
          query={positionsQuery}
          name="positions"
          emptyWhen={data !== undefined && data.positions.length === 0}
          empty={<p className="posscreen__empty">{emptyPositions}</p>}
        >
          {data?.positions.map((p) => (
            <PositionCard
              key={`${p.pair}-${p.decision_id}`}
              p={p}
              now={now}
              nearAtr={nearAtr}
              trailing={settings?.risk.trailing_stop_enabled ?? false}
              makerFee={settings?.paper.maker_fee_pct ?? "0.15"}
              onOpen={openDecision}
            />
          ))}
        </CardFetch>
      </section>

      <Card label="Working orders">
        <div className="posscreen__sectionhead">
          <h2 className="posscreen__h2">
            Working orders · {data?.orders.length ?? 0}
          </h2>
          <span className="posscreen__meta">
            {isLive ? "live" : (selectedShadow?.name ?? view)} · limit entries waiting for a fill
          </span>
        </div>
        <CardFetch
          query={positionsQuery}
          name="working orders"
          emptyWhen={data !== undefined && data.orders.length === 0}
          empty={<p className="posscreen__empty">{ordersEmpty}</p>}
        >
          {data && data.orders.length > 0 && (
            <div className="posscreen__orders">
              <div className="posscreen__orderhead posscreen__hide-phone">
                <span>Pair</span><span>Side</span><span>Qty</span><span>Limit</span>
                <span>Placed at</span><span>Cancels at</span><span>Last price</span>
              </div>
              {data.orders.map((o) => (
                <OrderRow key={o.id} o={o} />
              ))}
              <p className="posscreen__note">
                Post-only: it fills at the limit price exactly, with the maker fee, only when
                the price trades through it. It is cancelled at the pair's next cycle or when a
                gate blocks the entry; a cancelled entry is stored as unfilled, with its outcome
                tracked.
              </p>
            </div>
          )}
        </CardFetch>
      </Card>

      <div className="posscreen__duo">
        <Card label="Today">
          <div className="posscreen__sectionhead">
            <h2 className="posscreen__h2">Today</h2>
            <span className="posscreen__meta">live · since 00:00 Amsterdam</span>
          </div>
          <CardFetch query={positionsQuery} name="today" emptyWhen={false}>
            {data?.today && (
              <>
                <div className="posscreen__todaylist">
                  {data.today.items.map((item, i) => (
                    <TodayRow key={`${item.at}-${item.pair}-${i}`} item={item} onOpen={openDecision} />
                  ))}
                </div>
                <div className="posscreen__todaystats">
                  <span>Entries <span className="num">{data.today.entries}</span></span>
                  <span>Exits <span className="num">{data.today.exits}</span></span>
                  <span>
                    Realised{" "}
                    <span className={`num ${Number(data.today.realised) >= 0 ? "posscreen__up" : "posscreen__down"}`}>
                      {formatSignedEur(data.today.realised)}
                    </span>
                  </span>
                  <span>Fees <span className="num">{formatEur(data.today.fees)}</span></span>
                </div>
                <p className="posscreen__note">
                  Reasons: decision, stop, target, model exit, time stop, Stop &amp; flatten,
                  kill switch, or closed by you. The result is after both fees; R is against the
                  stop at entry.
                </p>
              </>
            )}
          </CardFetch>
        </Card>

        <Card label="Paper account">
          <div className="posscreen__sectionhead">
            <h2 className="posscreen__h2">Paper account</h2>
            {data?.paper_account && (
              <span className="posscreen__meta">
                live · #{data.paper_account.id} · since {formatDayShort(data.paper_account.started_at)}
              </span>
            )}
          </div>
          <CardFetch query={positionsQuery} name="paper account" emptyWhen={false}>
            {data?.paper_account && (
              <>
                <div className="posscreen__acct">
                  <AcctRow label="Starting balance" value={formatEur(data.paper_account.start_balance)} />
                  <AcctRow label="Cash" value={formatEur(data.paper_account.cash)} />
                  <AcctRow label="Coins value" value={formatEur(data.paper_account.coins_value)} />
                  <AcctRow label="Equity" value={formatEur(data.paper_account.equity)} />
                  <AcctRow
                    label={
                      <>
                        Realised P&amp;L{" "}
                        <span className="posscreen__meta">
                          · {data.paper_account.closed_trades} closed trades, after fees
                        </span>
                      </>
                    }
                    value={formatSignedEur(data.paper_account.realised_pnl)}
                    tone={Number(data.paper_account.realised_pnl) >= 0 ? "up" : "down"}
                  />
                  <AcctRow label="Fees paid" value={formatEur(data.paper_account.fees_total)} />
                  <AcctRow label="Slippage paid" value={formatEur(data.paper_account.slippage_total)} />
                </div>
                <p className="posscreen__note">
                  Simulated against Bitvavo prices: taker fee {settings?.paper.taker_fee_pct ?? "0.25"}%,
                  maker {settings?.paper.maker_fee_pct ?? "0.15"}%, market slippage{" "}
                  {settings?.paper.slippage_pct ?? "0.05"}%. "Reset paper account" in Settings
                  starts a new account; this one stays readable.
                </p>
              </>
            )}
          </CardFetch>
        </Card>
      </div>

      <Card label="Order history">
        <div className="posscreen__sectionhead">
          <h2 className="posscreen__h2">Order history</h2>
          <span className="posscreen__meta">
            live · every paper fill, newest first · times in Amsterdam, UTC on hover
          </span>
        </div>
        <div className="posscreen__filters">
          <label className="posscreen__field">
            Pair
            <select className="input" value={pair} onChange={(e) => setParam("pair", e.target.value)}>
              <option value="">Any pair</option>
              {pairs.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </label>
          <label className="posscreen__field">
            Side
            <select className="input" value={side} onChange={(e) => setParam("side", e.target.value)}>
              <option value="">Buy and sell</option>
              <option value="buy">Buy</option>
              <option value="sell">Sell</option>
            </select>
          </label>
          <label className="posscreen__field">
            Reason
            <select className="input" value={reason} onChange={(e) => setParam("reason", e.target.value)}>
              <option value="">Any reason</option>
              {REASON_OPTIONS.map(([code, label]) => (
                <option key={code} value={code}>{label}</option>
              ))}
            </select>
          </label>
          <label className="posscreen__field">
            Period
            <select className="input" value={period} onChange={(e) => setParam("period", e.target.value)}>
              <option value="30d">30 days</option>
              <option value="7d">7 days</option>
              <option value="all">Since start</option>
              <option value="custom">Custom</option>
            </select>
          </label>
          {period === "custom" && (
            <>
              <label className="posscreen__field">
                From
                <input
                  type="date"
                  className="input"
                  value={customFrom}
                  onChange={(e) => setParam("from", e.target.value)}
                />
              </label>
              <label className="posscreen__field">
                To
                <input
                  type="date"
                  className="input"
                  value={customTo}
                  onChange={(e) => setParam("to", e.target.value)}
                />
              </label>
            </>
          )}
          <button type="button" className="btn btn--secondary" onClick={clearFilters}>
            Clear filters
          </button>
          <a className="btn btn--secondary" href={exportHref} download>
            Export CSV
          </a>
        </div>

        <CardFetch
          query={fillsQuery}
          name="order history"
          emptyWhen={fillsQuery.isSuccess && fills.length === 0}
          empty={<p className="posscreen__empty">No fills match these filters.</p>}
        >
          {fills.length > 0 && (
            <div className="posscreen__fills">
              <div className="posscreen__fillhead posscreen__hide-phone">
                <span>Time</span><span>Pair</span><span>Side</span><span>Qty</span>
                <span>Reference</span><span>Fill</span><span>Slippage</span><span>Fee</span>
                <span>Reason</span><span>Belongs to</span>
              </div>
              {fills.map((f) => (
                <FillRow key={f.id} f={f} onOpen={openDecision} />
              ))}
            </div>
          )}
        </CardFetch>

        <div className="posscreen__fillfoot">
          <span className="posscreen__meta">
            {fills.length} of {fillsTotal} fills shown · {periodLabel(period)}
          </span>
          {fillsQuery.hasNextPage && (
            <button
              type="button"
              className="btn btn--secondary"
              onClick={() => void fillsQuery.fetchNextPage()}
            >
              Load older
            </button>
          )}
        </div>
        <p className="posscreen__note">
          Reference is the price the fill was simulated from: the last trade when a market order
          was sent, the watcher's price that crossed a stop, or the limit for a limit fill.
          Slippage is the cost of fill against reference for the whole quantity; limit fills have
          none. Fills from before this screen existed were rebuilt from trades and positions, so
          they show — for reference and slippage.
        </p>
      </Card>

      {decisionId && (
        <DecisionDrawer
          id={decisionId}
          onNavigate={(id) => setDecisionId(id)}
          onClose={() => setDecisionId(null)}
        />
      )}
    </AppShell>
  );
}

function periodLabel(period: string): string {
  return { "30d": "30 days", "7d": "7 days", all: "since start", custom: "custom" }[period] ?? period;
}

function Total({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "up" | "down";
}) {
  return (
    <span className="posscreen__total">
      {label}{" "}
      <span className={`num ${tone === "up" ? "posscreen__up" : tone === "down" ? "posscreen__down" : ""}`}>
        {value}
      </span>
    </span>
  );
}

function PositionCard({
  p,
  now,
  nearAtr,
  trailing,
  makerFee,
  onOpen,
}: {
  p: PositionCardOut;
  now: number;
  nearAtr: number;
  trailing: boolean;
  makerFee: string;
  onOpen: (id: string | null | undefined) => void;
}) {
  const entry = Number(p.entry);
  const stop = Number(p.stop);
  const target = Number(p.target);
  const last = Number(p.last);
  const span = target - stop || 1;
  const pct = (v: number) => Math.max(0, Math.min(100, ((v - stop) / span) * 100));
  const stopAbove = p.stop_above_entry;
  const hitUp = Number(p.stop_hit_eur) >= 0;
  const hitSub =
    p.stop_hit_r == null
      ? ""
      : hitUp
        ? `${signedR(p.stop_hit_r)} · covers both fees (${formatEur(p.round_trip_fees_eur)})`
        : [
            signedR(p.stop_hit_r),
            p.stop_hit_pct_equity != null ? `${formatQty(p.stop_hit_pct_equity)}% of equity` : null,
            "before fees",
          ]
            .filter(Boolean)
            .join(" · ");
  const histNote = (() => {
    if (p.entry_order_type === "limit") {
      return `Entered by a limit order: filled at ${formatPrice(p.entry)} exactly, maker fee ${formatQty(makerFee)}%.`;
    }
    const base = `Breakeven + fees once +1R${trailing ? "; trailing from +2R" : ""}.`;
    return p.stop_history.length > 1 ? base : `No moves yet. ${base}`;
  })();
  return (
    <article className="poscard">
      <div className="poscard__head">
        <div className="poscard__title">
          <b>{p.pair}</b>
          <span className="num posscreen__meta">
            {formatQty(p.qty)} @ {formatPrice(p.entry)}
          </span>
          <span className="num posscreen__meta">last {formatPrice(p.last)}</span>
          <span className={`num posscreen__pnl ${Number(p.unrealised) >= 0 ? "posscreen__up" : "posscreen__down"}`}>
            {formatSignedEur(p.unrealised)}
            {" · "}
            {p.pnl_r != null ? `${formatSigned(Number(p.pnl_r))}R` : "stop above entry"}
          </span>
        </div>
        <span className="posscreen__meta">{formatOpened(p.opened_at, now)} · held {formatHeld((now - new Date(p.opened_at).getTime()) / 1000)}</span>
      </div>

      <div className="poscard__barwrap">
        <div className="poscard__bar" aria-hidden="true">
          <i className="poscard__track" />
          <i
            className="poscard__fill"
            style={{
              left: `${Math.min(pct(entry), pct(last))}%`,
              width: `${Math.max(0.3, Math.abs(pct(last) - pct(entry)))}%`,
              background: last >= entry ? "var(--ta-positive)" : "var(--ta-negative)",
            }}
          />
          <i className="poscard__tick poscard__tick--stop" title="Stop" style={{ left: `calc(${pct(stop)}% - 1px)` }} />
          <i className="poscard__tick poscard__tick--entry" title="Entry" style={{ left: `calc(${pct(entry)}% - 1px)` }} />
          <i className="poscard__tick poscard__tick--target" title="Target" style={{ right: 0 }} />
          <i className="poscard__dot" title="Last price" style={{ left: `calc(${pct(last)}% - 5px)` }} />
        </div>
        <div className="poscard__labels num">
          <span className={stopAbove ? "poscard__up" : "poscard__down"}>stop {formatPrice(p.stop)}</span>
          <span className="poscard__mid">entry {formatPrice(p.entry)} · last {formatPrice(p.last)}</span>
          <span className="poscard__up">target {formatPrice(p.target)}</span>
        </div>
      </div>

      <div className="poscard__facts">
        <Fact label="If the stop hits" value={formatSignedEur(p.stop_hit_eur)} tone={hitUp ? "up" : "down"} sub={hitSub} />
        <Fact
          label="To the stop"
          value={p.stop_dist_pct != null ? `${formatQty(p.stop_dist_pct)}%` : "—"}
          tone={p.stop_near ? "amber" : undefined}
          sub={
            p.stop_dist_atr != null
              ? `${signedAtr(p.stop_dist_atr)}${p.stop_near ? ` · under ${formatQty(String(nearAtr))} ATR` : ""}`
              : ""
          }
        />
        <Fact
          label="To the target"
          value={p.target_dist_pct != null ? `${formatQty(p.target_dist_pct)}%` : "—"}
          sub={
            [
              p.target_dist_atr != null ? signedAtr(p.target_dist_atr) : null,
              p.rr_at_entry != null ? `R/R ${formatQty(p.rr_at_entry)} at entry` : null,
            ]
              .filter(Boolean)
              .join(" · ")
          }
        />
        <Fact
          label="Time stop"
          value={p.time_stop_at ? formatDayMonthTime(p.time_stop_at) : "—"}
          sub={
            p.time_stop_at
              ? `${Math.round((new Date(p.time_stop_at).getTime() - new Date(p.opened_at).getTime()) / 864e5)} days after entry`
              : ""
          }
        />
      </div>

      <div className="poscard__hist">
        <span className="poscard__histlabel">Stop history</span>
        <div className="poscard__histchips">
          {p.stop_history.map((h, i) => (
            <span key={`${h.kind}-${h.at}-${i}`} className={i === 0 ? "poschip" : "poschip poschip--move"}>
              {i > 0 ? "→ " : ""}
              {h.kind} {formatPrice(h.stop)} · {formatDayTime(h.at)}
            </span>
          ))}
        </div>
        <span className="posscreen__note">{histNote}</span>
      </div>

      <div className="poscard__foot">
        <span className="poscard__watcher">
          <span className={`poscard__dotwatch ${p.watcher_checked_at ? "" : "poscard__dotwatch--off"}`} aria-hidden="true" />
          {p.watcher_checked_at ? (
            <>
              Exit watcher checked{" "}
              <span className="num">
                {new Date(p.watcher_checked_at).toLocaleTimeString("en-GB", {
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                  timeZone: DISPLAY_TZ,
                })}
              </span>{" "}
              <span className="posscreen__meta">· stop, target and time stop every 30 s, also while paused</span>
            </>
          ) : (
            <span className="posscreen__meta">Exit watcher not checked since the restart</span>
          )}
        </span>
        {p.latest_decision && (
          <button type="button" className="poscard__decision" onClick={() => onOpen(p.latest_decision?.id)}>
            Latest decision: {p.latest_decision.action} · {formatDayTime(p.latest_decision.at)}
            {p.latest_decision.confidence != null ? ` · confidence ${p.latest_decision.confidence.toFixed(2)}` : ""}
            {p.latest_decision.limit_price != null ? ` · limit ${formatPrice(p.latest_decision.limit_price)}` : ""} →
          </button>
        )}
      </div>
    </article>
  );
}

function Fact({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "up" | "down" | "amber" }) {
  return (
    <div className="poscard__fact">
      <span className="poscard__factlabel">{label}</span>
      <span className={`num ${tone === "up" ? "posscreen__up" : tone === "down" ? "posscreen__down" : tone === "amber" ? "posscreen__amber" : ""}`}>
        {value}
      </span>
      {sub && <span className="posscreen__meta">{sub}</span>}
    </div>
  );
}

function OrderRow({ o }: { o: PaperOrderOut }) {
  const gap =
    o.limit_gap_pct != null && o.limit_gap_side != null
      ? `${formatPct(o.limit_gap_pct, 2)} ${o.limit_gap_side} the limit`
      : null;
  const placed = new Date(o.placed_at).toLocaleString("en-GB", {
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: DISPLAY_TZ,
  });
  return (
    <div className="posscreen__orderrow">
      <b>{o.pair}</b>
      <span className={`num ${o.side === "buy" ? "posscreen__up" : "posscreen__down"}`}>{o.side}</span>
      <span className="num">{formatQty(o.qty)}</span>
      <span className="num">{formatPrice(o.limit_price)}</span>
      <span className="num posscreen__wide">{placed}</span>
      <span className="posscreen__wide">
        <span className="num">{o.expires_at ? formatTime(o.expires_at) : "—"}</span>
        <span className="posscreen__meta"> · {o.leg === "entry" ? "next cycle, or when a gate blocks" : "rests until the position exits"}</span>
      </span>
      <span className="posscreen__wide">
        <span className="num">{o.last_price != null ? formatPrice(o.last_price) : "—"}</span>
        <span className="posscreen__meta">
          {gap ? ` · ${gap}` : ""}
          {o.min_price != null ? ` · low since placed ${formatPrice(o.min_price)}` : ""}
        </span>
      </span>
      <span className="posscreen__orderphone">
        {o.side} {formatQty(o.qty)} @ {formatPrice(o.limit_price)} · placed {placed} ·{" "}
        {o.last_price != null ? `last ${formatPrice(o.last_price)}` : "no last price"}
        {gap ? ` · ${gap}` : ""}
        {o.min_price != null ? ` · low since placed ${formatPrice(o.min_price)}` : ""}
      </span>
    </div>
  );
}

function TodayRow({ item, onOpen }: { item: TodayItemOut; onOpen: (id: string | null | undefined) => void }) {
  const buy = item.side === "buy";
  const sub = buy
    ? [
        reasonLabel(item.reason),
        item.confidence != null ? `confidence ${item.confidence.toFixed(2)}` : null,
        item.rr != null ? `R/R ${formatQty(item.rr)}` : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : [
        reasonLabel(item.reason),
        item.held_seconds != null ? `held ${formatHeld(item.held_seconds)}` : null,
        item.fees_eur != null ? `fees ${formatEur(item.fees_eur)}` : null,
      ]
        .filter(Boolean)
        .join(" · ");
  const right = buy ? (
    item.still_open ? (
      <span className="posscreen__meta">open</span>
    ) : null
  ) : item.realised_eur != null ? (
    <span className={`num ${Number(item.realised_eur) >= 0 ? "posscreen__up" : "posscreen__down"}`}>
      {formatSignedEur(item.realised_eur)}
      {item.realised_r != null ? ` · ${formatSigned(Number(item.realised_r))}R` : ""}
    </span>
  ) : null;
  return (
    <div className="posscreen__todayrow">
      <span className="posscreen__todaymain">
        <span>
          <span className="num posscreen__meta">{formatTime(item.at)}</span>{" "}
          <span className={`num posscreen__side ${buy ? "posscreen__up" : "posscreen__down"}`}>
            {buy ? "BUY" : "SELL"}
          </span>{" "}
          <b>{item.pair}</b>{" "}
          <span className="num posscreen__meta">
            {formatQty(item.qty)} @ {formatPrice(item.price)}
          </span>
        </span>
        <span className="posscreen__meta">{sub}</span>
      </span>
      <span className="posscreen__todayright">
        {right}
        <button type="button" className="posscreen__link" onClick={() => onOpen(item.decision_id)}>
          {item.trade_id != null ? `trade #${item.trade_id}` : "decision"} →
        </button>
      </span>
    </div>
  );
}

function AcctRow({ label, value, tone }: { label: ReactNode; value: string; tone?: "up" | "down" }) {
  return (
    <div className="posscreen__acctrow">
      <span>{label}</span>
      <span className={`num ${tone === "up" ? "posscreen__up" : tone === "down" ? "posscreen__down" : ""}`}>{value}</span>
    </div>
  );
}

function FillRow({ f, onOpen }: { f: FillOut; onOpen: (id: string | null | undefined) => void }) {
  const t = formatFillTime(f.at);
  const buy = f.side === "buy";
  const linkLabel =
    f.trade_id != null
      ? `trade #${f.trade_id}`
      : `decision ${f.decision_at ? formatDayTime(f.decision_at) : ""}`.trim();
  const reasonCls =
    f.reason === "stop"
      ? "posscreen__reason posscreen__reason--stop"
      : f.reason === "target"
        ? "posscreen__reason posscreen__reason--target"
        : "posscreen__reason";
  return (
    <div>
      <div className="posscreen__fillrow posscreen__hide-phone">
        <span title={t.utc} className="num posscreen__meta">{t.text}</span>
        <b>{f.pair}</b>
        <span className={`num ${buy ? "posscreen__up" : "posscreen__down"}`}>{buy ? "BUY" : "SELL"}</span>
        <span className="num posscreen__right">{formatQty(f.qty)}</span>
        <span className="num posscreen__right posscreen__meta">{f.reference_price != null ? formatPrice(f.reference_price) : "—"}</span>
        <span className="num posscreen__right">{formatPrice(f.fill_price)}</span>
        <span className="num posscreen__right posscreen__meta">{f.slippage_eur != null ? formatEur(f.slippage_eur) : "—"}</span>
        <span className="num posscreen__nowrap">{formatEur(f.fee_eur)} <span className="posscreen__meta">{f.fee_kind}</span></span>
        <span><span className={reasonCls}>{reasonLabel(f.reason)}</span></span>
        <button type="button" className="posscreen__link" onClick={() => onOpen(f.decision_id)}>
          {linkLabel} →
        </button>
      </div>
      <div className="posscreen__fillphone">
        <span className="posscreen__fillphonehead">
          <span className="num posscreen__meta">{t.text}</span>
          <b>{f.pair}</b>
          <span className={`num ${buy ? "posscreen__up" : "posscreen__down"}`}>{buy ? "BUY" : "SELL"} {formatQty(f.qty)}</span>
          <span className={reasonCls}>{reasonLabel(f.reason)}</span>
        </span>
        <span className="num posscreen__meta">
          fill {formatPrice(f.fill_price)} · ref {f.reference_price != null ? formatPrice(f.reference_price) : "—"} · slip{" "}
          {f.slippage_eur != null ? formatEur(f.slippage_eur) : "—"} · fee {formatEur(f.fee_eur)} {f.fee_kind}
        </span>
        <button type="button" className="posscreen__link" onClick={() => onOpen(f.decision_id)}>
          {linkLabel} →
        </button>
      </div>
    </div>
  );
}

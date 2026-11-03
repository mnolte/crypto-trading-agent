import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type {
  EntryPause,
  MarketOut,
  Settings as SettingsType,
  SettingsOut,
  SourceOut,
} from "../api/client";
import {
  useCosts,
  useCreateShadow,
  useDeleteData,
  useMarkets,
  usePrompts,
  usePromoteShadow,
  useReadiness,
  useRemoveSecret,
  useResetPaper,
  useSaveSecret,
  useSaveSettings,
  useSaveSmtpPassword,
  useSettings,
  useShadows,
  useSnapshot,
  useSources,
  useStopShadow,
  useTestNotifications,
  useTestSecret,
  useUpdateMarket,
  useUpdateSource,
} from "../api/hooks";
import { AppShell } from "../components/AppShell";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Chip } from "../components/Chip";
import { DataTable } from "../components/DataTable";
import { Dialog } from "../components/Dialog";
import { FormField, Input, Select } from "../components/FormField";
import { Meter } from "../components/Meter";
import { PauseSelect } from "../components/PauseSelect";
import { SegmentedControl } from "../components/SegmentedControl";
import {
  formatDateTime,
  formatEur,
  formatEurCompact,
  formatPct,
  formatSigned,
  formatTime,
  parseNumberInput,
} from "../lib/format";

/** GET returns decimal strings; PUT accepts them, so the draft is a deep clone. */
function toDraft(data: SettingsOut): SettingsType {
  return JSON.parse(JSON.stringify(data)) as SettingsType;
}

const FEATURE_LABEL: Record<string, string> = {
  decisions: "Decisions",
  ask: "Ask",
  briefings: "Briefings & reviews",
  shadows: "Shadows",
  replays: "Replays",
};

const WEEKLY_REVIEW_LABEL: Record<string, string> = {
  mon_08: "Monday 08:00",
  sun_20: "Sunday 20:00",
  off: "Off",
};

const NOTIFICATION_LABEL: Record<string, string> = {
  kill_switch: "Kill switch fired",
  daily_loss: "Daily loss limit reached",
  pair_stale: "Pair stale for 2 cycles",
  source_failing: "Data source failing",
  model_failing: "Model failing or budget reached",
  trade: "Trade opened or closed",
  risk_control: "Shock brake or risk throttle",
  review: "Morning briefing and weekly review",
};

export function Settings() {
  const { data } = useSettings();
  const { data: markets } = useMarkets();
  const { data: sources } = useSources();
  const { data: prompts } = usePrompts();
  const { data: snapshot } = useSnapshot();
  const { data: costs } = useCosts();
  const { data: readiness } = useReadiness();

  const [draft, setDraft] = useState<SettingsType | null>(null);
  const [original, setOriginal] = useState<SettingsType | null>(null);
  const [marketQuery, setMarketQuery] = useState("");
  const [marketError, setMarketError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [smtpPassword, setSmtpPassword] = useState("");

  const { data: shadows } = useShadows();
  const [shadowForm, setShadowForm] = useState(false);
  const [shadowModel, setShadowModel] = useState("");
  const [shadowPrompt, setShadowPrompt] = useState("decision.v1");
  const [shadowEntryOrders, setShadowEntryOrders] = useState("market");
  const [promoteShadow, setPromoteShadow] = useState<string | null>(null);
  const [shadowError, setShadowError] = useState<string | null>(null);

  const save = useSaveSettings();
  const createShadow = useCreateShadow();
  const stopShadow = useStopShadow();
  const promoteShadowMut = usePromoteShadow();
  const reset = useResetPaper();
  const deleteData = useDeleteData();
  const updateMarket = useUpdateMarket();
  const testNotifications = useTestNotifications();
  const saveSmtpPassword = useSaveSmtpPassword();

  useEffect(() => {
    if (data && original === null) {
      const d = toDraft(data);
      setDraft(d);
      setOriginal(d);
    }
  }, [data, original]);

  const dirty =
    draft !== null &&
    original !== null &&
    JSON.stringify(draft) !== JSON.stringify(original);

  const discard = () => original && setDraft(toDraft(original as SettingsOut));

  const set = <K extends keyof SettingsType>(key: K, value: SettingsType[K]) =>
    setDraft((d) => (d ? { ...d, [key]: value } : d));
  const setRisk = (key: string, value: unknown) =>
    setDraft((d) => (d ? { ...d, risk: { ...d.risk, [key]: value } } : d));
  const setModel = (key: string, value: unknown) =>
    setDraft((d) => (d ? { ...d, model: { ...d.model, [key]: value } } : d));
  const setPaper = (key: string, value: unknown) =>
    setDraft((d) => (d ? { ...d, paper: { ...d.paper, [key]: value } } : d));
  const setNotifications = (key: string, value: unknown) =>
    setDraft((d) =>
      d ? { ...d, notifications: { ...d.notifications, [key]: value } } : d,
    );
  const setLearning = (key: string, value: unknown) =>
    setDraft((d) =>
      d ? { ...d, learning: { ...d.learning, [key]: value } } : d,
    );
  const setReadiness = (key: string, value: unknown) =>
    setDraft((d) =>
      d ? { ...d, readiness: { ...d.readiness, [key]: value } } : d,
    );

  const setNumber = (value: string, apply: (parsed: number) => void) => {
    const parsed = parseNumberInput(value);
    if (parsed !== undefined) apply(parsed);
  };

  const filteredMarkets = useMemo(
    () =>
      (markets ?? []).filter((m) =>
        m.market.toLowerCase().includes(marketQuery.toLowerCase()),
      ),
    [markets, marketQuery],
  );

  const pausedMap = useMemo(
    () => new Map((snapshot?.paused ?? []).map((p) => [p.pair, p])),
    [snapshot],
  );

  const unsavedCount =
    draft && original
      ? JSON.stringify(draft) === JSON.stringify(original)
        ? 0
        : 1
      : 0;

  const unsavedBar = dirty && (
    <div role="region" aria-label="Unsaved changes" className="unsaved-bar">
      <span className="unsaved-bar__text">
        {unsavedCount} unsaved change{unsavedCount === 1 ? "" : "s"}
      </span>
      <Button variant="secondary" disabled={!dirty} onClick={discard}>
        Discard
      </Button>
      <Button
        variant="primary"
        disabled={!dirty || save.isPending}
        onClick={() =>
          draft &&
          save.mutate(draft, {
            onError: (e) => setSaveError(e.message),
            onSuccess: (fresh) => {
              setSaveError(null);
              const d = toDraft(fresh);
              setDraft(d);
              setOriginal(d);
            },
          })
        }
      >
        {save.isPending ? "Saving…" : "Save · applies next cycle"}
      </Button>
    </div>
  );

  return (
    <AppShell
      title="Settings"
      sub="Changes apply at the next cycle · every save is logged in History"
      controls={false}
    >
      <div className="settings-grid">
        {saveError && (
          <p className="settings-error" role="alert">
            {saveError}
          </p>
        )}

        <Card title="Markets &amp; schedule" heading="section" className="settings-card">
          <FormField label="Add a market" htmlFor="market-search">
            <Input
              id="market-search"
              type="search"
              placeholder="Search Bitvavo EUR markets, e.g. HYPE"
              value={marketQuery}
              onChange={(e) => setMarketQuery(e.target.value)}
            />
          </FormField>
          {marketError && (
            <p className="settings-error" role="alert">
              {marketError}
            </p>
          )}
          <DataTable
            minWidth={640}
            maxHeight={420}
            columns={[
              { key: "market", label: "Market" },
              { key: "vol", label: "30d vol/day", numeric: true },
              { key: "spread", label: "Spread", numeric: true },
              { key: "status", label: "Status" },
              { key: "pause", label: "Pause entries" },
              { key: "active", label: "Active", numeric: true },
            ]}
          >
            {filteredMarkets.map((m) => (
              <MarketRow
                key={m.market}
                market={m}
                pause={pausedMap.get(m.market) ?? pausedMap.get("*")}
                pending={updateMarket.isPending}
                onToggle={(active) =>
                  updateMarket.mutate(
                    { market: m.market, active },
                    {
                      onError: (e) => setMarketError(e.message),
                      onSuccess: () => setMarketError(null),
                    },
                  )
                }
              />
            ))}
          </DataTable>
          <div className="kv-box">
            <div className="kv">
              <span>
                <span className="num" style={{ color: "var(--ta-text)" }}>
                  {draft?.active_markets.length ?? 0}
                </span>{" "}
                active
              </span>
              <span>
                up to{" "}
                <span className="num" style={{ color: "var(--ta-text)" }}>
                  {draft?.model.daily_call_budget ?? "—"}
                </span>{" "}
                model calls/day
              </span>
            </div>
            <div className="kv">
              <span>Pause new entries on all coins</span>
              <PauseSelect pair="*" pause={pausedMap.get("*")} />
            </div>
          </div>
          <p className="card__meta" style={{ margin: 0, lineHeight: 1.5 }}>
            Eligible: 30-day average volume of at least{" "}
            {formatEurCompact(draft?.market.min_avg_daily_volume_eur ?? 0)} a
            day, spread at most{" "}
            {formatPct(draft?.market.max_avg_spread_pct ?? 0, 2)},{" "}
            {draft?.market.min_daily_candles ?? "—"}+ days of daily history, not
            a stablecoin. A market drops out only after{" "}
            {draft?.market.eligibility_fail_days ?? "—"} days below a threshold.
            Checked daily.
          </p>
          <div className="settings-row">
            <div className="field field--grow">
              <span className="field__label">Cycle</span>
              <span
                style={{
                  minHeight: 44,
                  display: "flex",
                  alignItems: "center",
                  color: "var(--ta-text-2)",
                }}
              >
                Every 15 minutes, {draft?.cycle_offset_s ?? 45} s after the
                quarter hour (fixed in v0.1)
              </span>
            </div>
            <FormField
              label="Execution"
              htmlFor="exec-mode"
              className="field--grow"
            >
              <Select
                id="exec-mode"
                value={draft?.execution_mode ?? "auto"}
                onChange={(e) =>
                  set("execution_mode", e.target.value as SettingsType["execution_mode"])
                }
              >
                <option value="auto">Automatic</option>
                <option value="supervised">Supervised (veto window)</option>
                <option value="manual">Manual approval</option>
              </Select>
            </FormField>
            <FormField
              label="Veto window (s, supervised only)"
              htmlFor="veto"
              className="field--grow"
            >
              <Input
                id="veto"
                mono
                type="number"
                value={draft?.veto_window_s ?? 60}
                onChange={(e) =>
                  setNumber(e.target.value, (v) => set("veto_window_s", v))
                }
              />
            </FormField>
          </div>
          <p className="card__meta" style={{ margin: 0, lineHeight: 1.5 }}>
            Trades the 1h timeframe with 4h and daily context. Stops and targets
            are checked every 30 s regardless of the cycle.
          </p>
        </Card>

        <Card title="Risk limits" heading="section" className="settings-card">
          <div className="settings-fields">
            <NumField label="Risk per trade (% equity)" value={draft?.risk.risk_per_trade_pct} onChange={(v) => setRisk("risk_per_trade_pct", v)} />
            <NumField label="Min. reward / risk" value={draft?.risk.min_rr} onChange={(v) => setRisk("min_rr", v)} />
            <NumField label="Max costs (% of risk)" value={draft?.risk.max_cost_pct_of_risk} onChange={(v) => setRisk("max_cost_pct_of_risk", v)} />
            <NumField label="Max total exposure (%)" value={draft?.risk.max_exposure_pct} onChange={(v) => setRisk("max_exposure_pct", v)} />
            <NumField label="Stop distance min (× ATR 1h)" value={draft?.risk.stop_min_atr} onChange={(v) => setRisk("stop_min_atr", v)} />
            <NumField label="Stop distance max (× ATR 1h)" value={draft?.risk.stop_max_atr} onChange={(v) => setRisk("stop_max_atr", v)} />
            <NumField label="Daily loss limit (%)" value={draft?.risk.daily_loss_limit_pct} onChange={(v) => setRisk("daily_loss_limit_pct", v)} />
            <NumField label="Kill at drawdown (%)" value={draft?.risk.kill_drawdown_pct} onChange={(v) => setRisk("kill_drawdown_pct", v)} danger />
            <PairFields
              label="Max correlated positions (corr. above)"
              a={draft?.risk.correlated_max_positions}
              b={draft?.risk.correlated_threshold}
              onA={(v) => setRisk("correlated_max_positions", v)}
              onB={(v) => setRisk("correlated_threshold", v)}
            />
            <NumField label="Shock brake: BTC move in 60 min (%)" value={draft?.risk.shock_brake_btc_move_pct} onChange={(v) => setRisk("shock_brake_btc_move_pct", v)} />
            <NumField label="Throttled risk per trade (%)" value={draft?.risk.throttled_risk_per_trade_pct} onChange={(v) => setRisk("throttled_risk_per_trade_pct", v)} />
            <PairFields
              label="Throttle after losses in a row / drawdown (%)"
              a={draft?.risk.throttle_consecutive_losses}
              b={draft?.risk.throttle_drawdown_pct}
              onA={(v) => setRisk("throttle_consecutive_losses", v)}
              onB={(v) => setRisk("throttle_drawdown_pct", v)}
            />
          </div>
          <label className="check-label">
            <input
              type="checkbox"
              checked={draft?.risk.trailing_stop_enabled ?? false}
              onChange={(e) => setRisk("trailing_stop_enabled", e.target.checked)}
            />
            <span>
              Trailing stop from +2R (2 × ATR below the highest close)
              <br />
              <span className="card__meta">
                Off by default. Test it in What-if or as a shadow strategy
                first.
              </span>
            </span>
          </label>
        </Card>

        <Card title="Decision model" heading="section" className="settings-card">
          <div className="settings-fields">
            <FormField label="Decider" htmlFor="decider">
              <Select
                id="decider"
                value={draft?.model.decider ?? "openai"}
                onChange={(e) => setModel("decider", e.target.value)}
              >
                <option value="openai">OpenAI</option>
                <option value="rules">Rules baseline</option>
              </Select>
            </FormField>
            <FormField label="Model" htmlFor="model-id">
              <Input
                id="model-id"
                mono
                value={draft?.model.model_id ?? ""}
                onChange={(e) => setModel("model_id", e.target.value)}
              />
            </FormField>
            <FormField label="Prompt version" htmlFor="prompt-version">
              <Select
                id="prompt-version"
                value={draft?.model.prompt_version ?? "decision.v1"}
                onChange={(e) => setModel("prompt_version", e.target.value)}
              >
                {(prompts ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.id}
                  </option>
                ))}
                {prompts &&
                  !prompts.some((p) => p.id === draft?.model.prompt_version) &&
                  draft?.model.prompt_version && (
                    <option value={draft.model.prompt_version}>
                      {draft.model.prompt_version}
                    </option>
                  )}
              </Select>
            </FormField>
            <NumField label="Daily call budget" value={draft?.model.daily_call_budget} onChange={(v) => setModel("daily_call_budget", v)} int />
          </div>
          <label className="check-label">
            <input
              type="checkbox"
              checked={draft?.model.rules_prefilter ?? true}
              onChange={(e) => setModel("rules_prefilter", e.target.checked)}
            />
            <span>
              Only call the model when Rules sees a possible setup or a position
              is open
            </span>
          </label>
          <div className="settings-row">
            <NumField label="Monthly cost cap (€)" value={draft?.model.monthly_cost_cap_eur} onChange={(v) => setModel("monthly_cost_cap_eur", v)} className="field--grow" />
            <div className="field field--grow">
              <span className="field__label">This month</span>
              <Meter
                pct={
                  snapshot && Number(snapshot.usage.cap_eur) > 0
                    ? (Number(snapshot.usage.cost_month_eur) /
                        Number(snapshot.usage.cap_eur)) *
                      100
                    : 0
                }
                color="accent"
                large
              />
              <span className="card__meta">
                {snapshot
                  ? `${formatEur(snapshot.usage.cost_month_eur)} of ${formatEur(snapshot.usage.cap_eur)}`
                  : ""}
              </span>
              <span className="card__meta" style={{ fontSize: "var(--ta-text-sm)" }}>
                {costs && Object.keys(costs.by_feature).length > 0
                  ? `${Object.entries(costs.by_feature)
                      .map(
                        ([feature, eur]) =>
                          `${FEATURE_LABEL[feature] ?? feature} ${formatEur(eur)}`,
                      )
                      .join(" · ")}. `
                  : ""}
                At 80%: shadows, Ask and replays pause. At 100%: decisions use
                Rules until the 1st.
              </span>
            </div>
          </div>
          <p className="card__meta" style={{ margin: 0 }}>
            Costs are converted from USD at the day's rate. The OpenAI key is
            managed under <a href="#sources">Data sources &amp; API keys</a>.
          </p>
        </Card>

        <Card title="Market data &amp; paper account" heading="section" className="settings-card">
          <FormField label="Data source" htmlFor="data-source">
            <Select
              id="data-source"
              value={draft?.data_source ?? "bitvavo"}
              onChange={(e) =>
                set("data_source", e.target.value as SettingsType["data_source"])
              }
            >
              <option value="bitvavo">
                Bitvavo live prices (public API, no key)
              </option>
              <option value="sim">Simulated market</option>
            </Select>
          </FormField>
          <div className="settings-fields">
            <NumField label="Starting balance (€)" value={draft?.paper.start_balance_eur} onChange={(v) => setPaper("start_balance_eur", v)} />
            <NumField label="Slippage on market orders (%)" value={draft?.paper.slippage_pct} onChange={(v) => setPaper("slippage_pct", v)} />
            <NumField label="Taker fee per side (%)" value={draft?.paper.taker_fee_pct} onChange={(v) => setPaper("taker_fee_pct", v)} />
            <NumField label="Maker fee per side (%)" value={draft?.paper.maker_fee_pct} onChange={(v) => setPaper("maker_fee_pct", v)} />
          </div>
          <FormField label="Entry orders">
            <SegmentedControl
              label="Entry orders"
              value={draft?.paper.entry_orders ?? "market"}
              onChange={(v) =>
                setPaper("entry_orders", v as SettingsType["paper"]["entry_orders"])
              }
              options={[
                { value: "market", label: "Market" },
                { value: "limit", label: "Limit (post-only)" },
              ]}
            />
          </FormField>
          <p className="card__meta" style={{ margin: 0 }}>
            Limit: entries and targets pay the maker fee and may not fill; an
            unfilled entry is cancelled at the next cycle. Stops and forced
            exits always use market orders. Min. stop: 2.0% with market, 1.5%
            with limit entries. Test it as Shadow B first.
          </p>
          <div className="settings-row">
            <a className="btn btn--secondary" href="/api/export/trades.csv" download>
              Export trades (CSV)
            </a>
            <a className="btn btn--secondary" href="/api/export/decisions.csv" download>
              Export decisions (CSV)
            </a>
            <Button variant="danger" onClick={() => setResetOpen(true)}>
              Reset paper account…
            </Button>
            <Button variant="danger-outline" onClick={() => setDeleteOpen(true)}>
              Delete all data…
            </Button>
          </div>
        </Card>

        <Card title="Notifications" heading="section" className="settings-card">
          <div className="settings-row">
            <label className="check-label">
              <input
                type="checkbox"
                checked={draft?.notifications.browser_enabled ?? false}
                onChange={(e) => {
                  setNotifications("browser_enabled", e.target.checked);
                  if (e.target.checked && "Notification" in window) {
                    void Notification.requestPermission();
                  }
                }}
              />
              <span>Browser notifications</span>
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={draft?.notifications.email_enabled ?? false}
                onChange={(e) => setNotifications("email_enabled", e.target.checked)}
              />
              <span>Email</span>
            </label>
          </div>
          <DataTable
            columns={[
              { key: "event", label: "Event" },
              { key: "browser", label: "Browser", numeric: true },
              { key: "email", label: "Email", numeric: true },
            ]}
          >
            {Object.entries(draft?.notifications.events ?? {}).map(([event, channels]) => (
              <tr key={event}>
                <td>{NOTIFICATION_LABEL[event] ?? event}</td>
                {(["browser", "email"] as const).map((channel) => (
                  <td className="num" key={channel}>
                    <input
                      type="checkbox"
                      aria-label={`${event} ${channel}`}
                      checked={channels[channel]}
                      onChange={(e) =>
                        setNotifications("events", {
                          ...draft?.notifications.events,
                          [event]: { ...channels, [channel]: e.target.checked },
                        })
                      }
                    />
                  </td>
                ))}
              </tr>
            ))}
          </DataTable>
          <div className="settings-fields">
            <FormField label="SMTP host"><Input value={draft?.notifications.smtp.host ?? ""} onChange={(e) => setNotifications("smtp", { ...draft?.notifications.smtp, host: e.target.value })} /></FormField>
            <NumField label="Port" value={draft?.notifications.smtp.port} int onChange={(v) => setNotifications("smtp", { ...draft?.notifications.smtp, port: v })} />
            <FormField label="TLS"><label className="check-label"><input type="checkbox" checked={draft?.notifications.smtp.tls ?? true} onChange={(e) => setNotifications("smtp", { ...draft?.notifications.smtp, tls: e.target.checked })} /><span>STARTTLS</span></label></FormField>
            <FormField label="Username"><Input value={draft?.notifications.smtp.username ?? ""} onChange={(e) => setNotifications("smtp", { ...draft?.notifications.smtp, username: e.target.value })} /></FormField>
            <FormField label="Sender"><Input value={draft?.notifications.smtp.sender ?? ""} onChange={(e) => setNotifications("smtp", { ...draft?.notifications.smtp, sender: e.target.value })} /></FormField>
            <FormField label="Recipient"><Input value={draft?.notifications.smtp.recipient ?? ""} onChange={(e) => setNotifications("smtp", { ...draft?.notifications.smtp, recipient: e.target.value })} /></FormField>
            <FormField label="SMTP password"><span style={{ display: "flex", gap: 8 }}><Input type="password" value={smtpPassword} placeholder="Stored in OS keyring" onChange={(e) => setSmtpPassword(e.target.value)} /><Button variant="secondary" disabled={!smtpPassword || saveSmtpPassword.isPending} onClick={() => saveSmtpPassword.mutate(smtpPassword, { onSuccess: () => setSmtpPassword("") })}>Save</Button></span></FormField>
          </div>
          <div className="settings-row">
            <Button
              variant="secondary"
              disabled={testNotifications.isPending}
              onClick={() => testNotifications.mutate()}
            >
              {testNotifications.isPending ? "Sending…" : "Send test"}
            </Button>
            {testNotifications.data && (
              <span className="card__meta" role="status">
                Test sent{testNotifications.data.browser ? " · browser" : ""}{testNotifications.data.email ? " · email" : ""}
              </span>
            )}
          </div>
          <p className="card__meta" style={{ margin: 0 }}>
            Event defaults follow the safety policy. The SMTP password is stored in the OS keyring and is never returned.
          </p>
        </Card>

        <Card
          title="Data sources &amp; API keys"
          heading="section"
          className="settings-card settings-card--wide"
          label="Data sources and API keys"
          actions={
            <span className="card__meta" style={{ maxWidth: 480 }}>
              Keys are stored in the OS keyring and never shown again; only the
              last 4 characters are displayed.
            </span>
          }
        >
          <span id="sources" style={{ position: "absolute", marginTop: -80 }} />
          <DataTable
            minWidth={860}
            columns={[
              { key: "source", label: "Source" },
              { key: "use", label: "Used for" },
              { key: "key", label: "API key" },
              { key: "status", label: "Status" },
              { key: "on", label: "On", numeric: true },
            ]}
          >
            {(sources ?? []).map((s) => (
              <SourceRow key={s.id} source={s} />
            ))}
          </DataTable>
        </Card>

        <Card
          title="Learning and explanations"
          heading="section"
          className="settings-card settings-card--wide"
          label="Learning and explanations"
        >
          <div className="settings-row">
            <label className="check-label field--grow">
              <input
                type="checkbox"
                checked={draft?.learning.plain_language ?? true}
                onChange={(e) =>
                  setLearning("plain_language", e.target.checked)
                }
              />
              <span>
                Plain-language mode
                <br />
                <span className="card__meta">
                  Explains jargon in tooltips and writes summaries for a
                  non-trader
                </span>
              </span>
            </label>
            <label className="check-label field--grow">
              <input
                type="checkbox"
                checked={draft?.learning.quiet_line ?? true}
                onChange={(e) => setLearning("quiet_line", e.target.checked)}
              />
              <span>
                Show &ldquo;why so quiet?&rdquo; line on the Dashboard
                <br />
                <span className="card__meta">
                  Says what the agent did this cycle and why it didn&apos;t
                  trade
                </span>
              </span>
            </label>
          </div>
          <div className="settings-fields">
            <FormField label="Morning briefing at" htmlFor="briefing-time">
              <Input
                id="briefing-time"
                mono
                type="time"
                value={draft?.learning.briefing_time ?? "08:00"}
                onChange={(e) => setLearning("briefing_time", e.target.value)}
              />
            </FormField>
            <FormField label="Weekly review on" htmlFor="weekly-review">
              <Select
                id="weekly-review"
                value={draft?.learning.weekly_review ?? "mon_08"}
                onChange={(e) => setLearning("weekly_review", e.target.value)}
              >
                {Object.entries(WEEKLY_REVIEW_LABEL).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            </FormField>
            <NumField
              label="Ask the agent · questions per day"
              value={draft?.learning.ask_daily_limit}
              int
              onChange={(v) => setLearning("ask_daily_limit", v)}
            />
            <NumField
              label="Insights · decisions for a finding"
              value={draft?.learning.insights_min_decisions}
              int
              onChange={(v) => setLearning("insights_min_decisions", v)}
            />
          </div>
          <p className="card__meta" style={{ margin: 0, lineHeight: 1.5 }}>
            Briefings, reviews and answers are written by the model from
            numbers computed in code. A check rejects any text with a number
            that isn&apos;t in the facts. Ask, briefings and reviews count
            toward the monthly cost cap.
          </p>
        </Card>

        <Card
          title="Shadow strategies"
          heading="section"
          className="settings-card settings-card--wide"
          label="Shadow strategies"
          actions={
            <Button
              size="sm"
              disabled={
                (shadows?.items.filter((s) => s.status === "running")
                  .length ?? 0) >= 2
              }
              onClick={() => {
                setShadowModel(draft?.model.model_id ?? "");
                setShadowPrompt(draft?.model.prompt_version ?? "decision.v1");
                setShadowEntryOrders("market");
                setShadowError(null);
                setShadowForm(true);
              }}
            >
              Add shadow (
              {shadows?.items.filter((s) => s.status === "running").length ?? 0}{" "}
              of 2)
            </Button>
          }
        >
          <p className="card__meta" style={{ margin: 0 }}>
            A shadow runs on the same data every cycle and records what it
            would have done, with its own paper account. It never places
            orders. Each one roughly doubles model calls on cycles where it
            runs.
          </p>
          {(shadows?.items ?? []).map((s) => (
            <div key={s.id} className="shadow-row">
              <div className="shadow-row__main">
                <span className="shadow-row__name">
                  {s.name}
                  {s.entry_orders === "limit" && (
                    <Chip variant="side">v0.2</Chip>
                  )}
                </span>
                <span className="card__meta">
                  {s.model} · {s.prompt_version}
                  {s.entry_orders === "limit" ? " · limit entries" : ""} · since{" "}
                  {formatDateTime(s.created_at)}
                  {s.paused ? " · paused (cost cap)" : ""}
                </span>
              </div>
              <div
                className="card__meta shadow-row__stat"
                title={
                  s.entry_orders === "limit" && s.orders_placed > 0
                    ? `Entries filled of placed.${
                        s.orders_placed - s.orders_filled > 0
                          ? ` The ${s.orders_placed - s.orders_filled} unfilled entries` +
                            (s.missed_fill_avg_r != null
                              ? ` would have averaged ${formatSigned(Number(s.missed_fill_avg_r))}R (missed fills).`
                              : " have no outcome data yet.")
                          : ""
                      }`
                    : undefined
                }
              >
                {s.return_pct != null ? `${formatSigned(s.return_pct)}%` : "—"}
                {" vs main "}
                {s.live_return_pct != null
                  ? `${formatSigned(s.live_return_pct)}%`
                  : "—"}
                {" · "}
                {s.decisions} trades
                {s.entry_orders === "limit" &&
                  ` · ${s.orders_filled}/${s.orders_placed} filled`}
              </div>
              <div className="shadow-row__actions">
                <Link to="/inspector" className="btn btn--secondary btn--sm">
                  Compare
                </Link>
                {s.status === "running" && (
                  <>
                    <Button size="sm" onClick={() => setPromoteShadow(s.id)}>
                      Promote…
                    </Button>
                    <Button
                      size="sm"
                      style={{ color: "var(--ta-negative-text)" }}
                      onClick={() => stopShadow.mutate(s.id)}
                    >
                      Stop
                    </Button>
                  </>
                )}
              </div>
            </div>
          ))}
          <p className="card__meta" style={{ margin: 0 }}>
            Promote replaces the main prompt, model or entry order type after
            a confirmation. The old version stays in the inspector history.
          </p>
        </Card>

        <Card
          title="Live readiness"
          heading="section"
          className="settings-card settings-card--wide"
          label="Live readiness"
          actions={
            readiness && (
              <Link to="/readiness" className="btn btn--secondary">
                Open checklist · {readiness.met} of {readiness.total} met
              </Link>
            )
          }
        >
          <div className="settings-fields">
            <NumField
              label="Min. closed trades"
              value={draft?.readiness.min_closed_trades}
              int
              onChange={(v) => setReadiness("min_closed_trades", v)}
            />
            <NumField
              label="Min. days running"
              value={draft?.readiness.min_days_running}
              int
              onChange={(v) => setReadiness("min_days_running", v)}
            />
            <NumField
              label="Max. drawdown (%)"
              value={draft?.readiness.max_drawdown_pct}
              onChange={(v) => setReadiness("max_drawdown_pct", v)}
            />
            <NumField
              label="Max. calibration gap (pts)"
              value={draft?.readiness.calibration_gap_pts}
              int
              onChange={(v) => setReadiness("calibration_gap_pts", v)}
            />
          </div>
          <p className="card__meta" style={{ margin: 0 }}>
            Other criteria (average R, profit factor, beats BTC, no kill, no
            errors) use the defaults in the spec. Loosening any threshold is
            logged.
          </p>
        </Card>
        {unsavedBar}
      </div>

      {resetOpen && (
        <Dialog
          title="Reset paper account?"
          onClose={() => setResetOpen(false)}
          actions={[
            { label: "Cancel", onClick: () => setResetOpen(false) },
            {
              label: "Reset account",
              variant: "primary",
              onClick: () =>
                reset.mutate(undefined, {
                  onSettled: () => setResetOpen(false),
                }),
            },
          ]}
        >
          <p>
            Closes all positions and starts paper account #
            {(snapshot?.account_id ?? 0) + 1} from{" "}
            {formatEur(draft?.paper.start_balance_eur ?? 10000, 0)}. Decisions,
            LLM calls, verdicts and outcomes are kept and tagged with account #
            {snapshot?.account_id ?? 1}; trades, the equity curve, Performance
            and Readiness start fresh. Export history first if you want it.
          </p>
        </Dialog>
      )}

      {shadowForm && (
        <Dialog
          title="Add a shadow strategy"
          onClose={() => setShadowForm(false)}
          actions={[
            { label: "Cancel", onClick: () => setShadowForm(false) },
            {
              label: "Add shadow",
              variant: "primary",
              onClick: () =>
                createShadow.mutate(
                  {
                    model: shadowModel.trim(),
                    prompt_version: shadowPrompt,
                    overrides:
                      shadowEntryOrders === "limit"
                        ? { "paper.entry_orders": "limit" }
                        : {},
                    daily_call_limit: 300,
                  },
                  {
                    onSuccess: () => setShadowForm(false),
                    onError: (e) =>
                      setShadowError(
                        e instanceof Error ? e.message : String(e),
                      ),
                  },
                ),
            },
          ]}
        >
          <div className="settings-fields">
            <FormField label="Model" htmlFor="shadow-model">
              <Input
                id="shadow-model"
                mono
                value={shadowModel}
                onChange={(e) => setShadowModel(e.target.value)}
              />
            </FormField>
            <FormField label="Prompt version" htmlFor="shadow-prompt">
              <Select
                id="shadow-prompt"
                value={shadowPrompt}
                onChange={(e) => setShadowPrompt(e.target.value)}
              >
                {(prompts ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.id}
                  </option>
                ))}
              </Select>
            </FormField>
            <FormField label="Entry orders">
              <SegmentedControl
                label="Entry orders"
                value={shadowEntryOrders}
                onChange={setShadowEntryOrders}
                options={[
                  { value: "market", label: "Market" },
                  { value: "limit", label: "Limit (post-only)" },
                ]}
              />
            </FormField>
          </div>
          <p className="card__meta" style={{ margin: 0 }}>
            The shadow starts flat, with cash equal to the live account&apos;s
            current equity, and is compared with the live account from that
            moment on.
          </p>
          {shadowError && <p className="field__msg">{shadowError}</p>}
        </Dialog>
      )}

      {promoteShadow && (
        <Dialog
          danger
          title="Promote this shadow?"
          confirmText="PROMOTE"
          onClose={() => setPromoteShadow(null)}
          actions={[
            { label: "Cancel", onClick: () => setPromoteShadow(null) },
            {
              label: "Promote",
              variant: "danger-solid",
              requiresConfirm: true,
              onClick: () =>
                promoteShadowMut.mutate(promoteShadow, {
                  onSettled: () => setPromoteShadow(null),
                }),
            },
          ]}
        >
          <p>
            The live decider switches to this shadow&apos;s model and prompt
            version, and the shadow stops. The old version stays in the
            inspector history.
          </p>
        </Dialog>
      )}

      {deleteOpen && (
        <Dialog
          danger
          title="Delete all data"
          confirmText="DELETE"
          onClose={() => setDeleteOpen(false)}
          actions={[
            { label: "Cancel", onClick: () => setDeleteOpen(false) },
            {
              label: "Delete everything",
              variant: "danger-solid",
              requiresConfirm: true,
              onClick: () =>
                deleteData.mutate(undefined, {
                  onSettled: () => setDeleteOpen(false),
                }),
            },
          ]}
        >
          <p>
            Deletes every account, trade, decision, model call, verdict,
            outcome, briefing and Ask conversation. Settings and API keys stay.
            This cannot be undone.
          </p>
        </Dialog>
      )}
    </AppShell>
  );
}

function NumField({
  label,
  value,
  onChange,
  int = false,
  danger = false,
  className = "",
}: {
  label: string;
  value: string | number | undefined;
  onChange: (value: number) => void;
  int?: boolean;
  danger?: boolean;
  className?: string;
}) {
  return (
    <FormField label={label} className={className}>
      <Input
        mono
        type="number"
        step={int ? 1 : "any"}
        invalid={danger}
        value={value ?? ""}
        onChange={(e) => {
          const parsed = parseNumberInput(e.target.value);
          if (parsed !== undefined) onChange(parsed);
        }}
      />
    </FormField>
  );
}

function PairFields({
  label,
  a,
  b,
  onA,
  onB,
}: {
  label: string;
  a: string | number | undefined;
  b: string | number | undefined;
  onA: (value: number) => void;
  onB: (value: number) => void;
}) {
  return (
    <FormField label={label}>
      <span style={{ display: "flex", gap: 8 }}>
        <Input
          mono
          type="number"
          step="any"
          value={a ?? ""}
          onChange={(e) => {
            const parsed = parseNumberInput(e.target.value);
            if (parsed !== undefined) onA(parsed);
          }}
        />
        <Input
          mono
          type="number"
          step="any"
          value={b ?? ""}
          onChange={(e) => {
            const parsed = parseNumberInput(e.target.value);
            if (parsed !== undefined) onB(parsed);
          }}
        />
      </span>
    </FormField>
  );
}

function MarketRow({
  market,
  pause,
  pending,
  onToggle,
}: {
  market: MarketOut;
  pause: EntryPause | undefined;
  pending: boolean;
  onToggle: (active: boolean) => void;
}) {
  const status = market.paused
    ? { text: "paused", variant: "amber" as const }
    : market.active
      ? market.warming_up
        ? { text: "warming up", variant: "amber" as const }
        : { text: "active", variant: "up" as const }
      : market.eligible
        ? { text: "eligible", variant: "neutral" as const }
        : { text: market.failed_rule ?? "ineligible", variant: "down" as const };
  return (
    <tr>
      <td className="text--strong">{market.market}</td>
      <td className="num">
        {market.volume_30d_eur !== null
          ? formatEurCompact(market.volume_30d_eur)
          : "—"}
      </td>
      <td className="num">
        {market.spread_pct !== null ? formatPct(market.spread_pct, 2) : "—"}
      </td>
      <td>
        <Chip variant={status.variant}>{status.text}</Chip>
      </td>
      <td>
        <PauseSelect pair={market.market} pause={pause} />
      </td>
      <td className="num">
        <button
          type="button"
          role="switch"
          aria-checked={market.active}
          aria-label={`${market.market} active`}
          className="switch"
          disabled={pending}
          onClick={() => onToggle(!market.active)}
        />
      </td>
    </tr>
  );
}

function SourceRow({ source }: { source: SourceOut }) {
  const updateSource = useUpdateSource();
  const saveSecret = useSaveSecret();
  const removeSecret = useRemoveSecret();
  const testSecret = useTestSecret();
  const [editing, setEditing] = useState(false);
  const [key, setKey] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  const needsKey = source.key_requirement !== "none";
  const status = !source.enabled
    ? { text: "disabled", variant: "neutral" as const }
    : source.last_error
      ? { text: "failing", variant: "down" as const }
      : source.key_requirement === "required" && !source.key_set
        ? { text: "key missing", variant: "amber" as const }
        : { text: "OK", variant: "up" as const };

  const saveKey = () =>
    saveSecret.mutate(
      { id: source.id, key },
      {
        onError: (e) => setMessage(e.message),
        onSuccess: () =>
          testSecret.mutate(source.id, {
            onSuccess: (r) => {
              setMessage(r.ok ? "Key saved and verified" : r.error ?? "Key test failed");
              if (r.ok) {
                setEditing(false);
                setKey("");
              }
            },
            onError: (e) => setMessage(e.message),
          }),
      },
    );

  return (
    <tr>
      <td className="text--strong">{source.name}</td>
      <td style={{ color: "var(--ta-text-2)" }}>{source.purpose}</td>
      <td>
        {needsKey ? (
          editing || !source.key_set ? (
            <span style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <Input
                type="password"
                aria-label={`${source.name} API key`}
                placeholder={
                  source.key_set ? "New key (replaces the stored one)" : "Paste key"
                }
                value={key}
                onChange={(e) => setKey(e.target.value)}
                style={{ height: 36, width: 200 }}
              />
              <Button
                variant="secondary"
                size="xs"
                disabled={!key.trim() || saveSecret.isPending}
                onClick={saveKey}
              >
                Save &amp; test
              </Button>
              {source.key_set && (
                <Button variant="ghost" size="xs" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              )}
            </span>
          ) : (
            <span style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <Chip variant={source.key_requirement === "required" ? "amber" : "neutral"}>
                {source.key_requirement}
              </Chip>
              <span className="num">••••{source.key_last4 ?? ""}</span>
              <Button
                variant="secondary"
                size="xs"
                disabled={testSecret.isPending}
                onClick={() =>
                  testSecret.mutate(source.id, {
                    onSuccess: (r) =>
                      setMessage(r.ok ? "Key verified" : r.error ?? "Key test failed"),
                    onError: (e) => setMessage(e.message),
                  })
                }
              >
                Test
              </Button>
              <Button variant="secondary" size="xs" onClick={() => setEditing(true)}>
                Replace
              </Button>
              <Button
                variant="ghost"
                size="xs"
                onClick={() =>
                  removeSecret.mutate(source.id, {
                    onError: (e) => setMessage(e.message),
                  })
                }
              >
                Remove
              </Button>
            </span>
          )
        ) : (
          <Chip variant="neutral">no key needed</Chip>
        )}
        {message && (
          <div className="card__meta" role="status" style={{ marginTop: 4 }}>
            {message}
          </div>
        )}
      </td>
      <td>
        <Chip variant={status.variant}>{status.text}</Chip>{" "}
        <span className="num card__meta">
          {source.last_success_at ? formatTime(source.last_success_at) : ""}
          {source.failures > 0 ? ` · ${source.failures} failures` : ""}
        </span>
      </td>
      <td className="num">
        <button
          type="button"
          role="switch"
          aria-checked={source.enabled}
          aria-label={`${source.name} enabled`}
          className="switch"
          disabled={updateSource.isPending}
          onClick={() =>
            updateSource.mutate({ id: source.id, enabled: !source.enabled })
          }
        />
      </td>
    </tr>
  );
}

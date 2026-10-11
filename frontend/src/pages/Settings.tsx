import { useEffect, useMemo, useState } from "react";
import type {
  MarketOut,
  Settings as SettingsType,
  SettingsOut,
  SourceOut,
} from "../api/client";
import {
  useCosts,
  useDeleteData,
  useMarkets,
  usePrompts,
  useRemoveSecret,
  useResetPaper,
  useSaveSecret,
  useSaveSettings,
  useSettings,
  useSnapshot,
  useSources,
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
import {
  formatEur,
  formatEurCompact,
  formatPct,
  formatTime,
  parseNumberInput,
} from "../lib/format";

/** GET returns decimal strings; PUT accepts them, so the draft is a deep clone. */
function toDraft(data: SettingsOut): SettingsType {
  return JSON.parse(JSON.stringify(data)) as SettingsType;
}

const FEATURE_LABEL: Record<string, string> = {
  decision: "Decisions",
  replay: "Replays",
};

export function Settings() {
  const { data } = useSettings();
  const { data: markets } = useMarkets();
  const { data: sources } = useSources();
  const { data: prompts } = usePrompts();
  const { data: snapshot } = useSnapshot();
  const { data: costs } = useCosts();

  const [draft, setDraft] = useState<SettingsType | null>(null);
  const [original, setOriginal] = useState<SettingsType | null>(null);
  const [marketQuery, setMarketQuery] = useState("");
  const [marketError, setMarketError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const save = useSaveSettings();
  const reset = useResetPaper();
  const deleteData = useDeleteData();
  const updateMarket = useUpdateMarket();

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

  const actions = (
    <>
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
    </>
  );

  return (
    <AppShell title="Settings" actions={actions} controls={false}>
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
            minWidth={520}
            columns={[
              { key: "market", label: "Market" },
              { key: "vol", label: "30d vol/day", numeric: true },
              { key: "spread", label: "Spread", numeric: true },
              { key: "status", label: "Status" },
              { key: "active", label: "Active", numeric: true },
            ]}
          >
            {filteredMarkets.map((m) => (
              <MarketRow
                key={m.market}
                market={m}
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
                <span className="num" style={{ color: "var(--text)" }}>
                  {draft?.active_markets.length ?? 0}
                </span>{" "}
                active
              </span>
              <span>
                up to{" "}
                <span className="num" style={{ color: "var(--text)" }}>
                  {draft?.model.daily_call_budget ?? "—"}
                </span>{" "}
                model calls/day
              </span>
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
                  color: "var(--text-soft)",
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
              hint="Supervised and manual need the approval flow, which arrives in a later step."
            >
              <Select
                id="exec-mode"
                value={draft?.execution_mode ?? "auto"}
                onChange={(e) =>
                  set("execution_mode", e.target.value as SettingsType["execution_mode"])
                }
              >
                <option value="auto">Automatic</option>
                <option
                  value="supervised"
                  disabled
                  title="Approval flow arrives in a later build step"
                >
                  Supervised (veto window)
                </option>
                <option
                  value="manual"
                  disabled
                  title="Approval flow arrives in a later build step"
                >
                  Manual approval
                </option>
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
              <span className="card__meta" style={{ fontSize: "var(--fs-caption)" }}>
                {costs && Object.keys(costs.by_feature).length > 0
                  ? `${Object.entries(costs.by_feature)
                      .map(
                        ([feature, eur]) =>
                          `${FEATURE_LABEL[feature] ?? feature} ${formatEur(eur)}`,
                      )
                      .join(" · ")}. `
                  : ""}
                At 80%: replays pause. At 100%: decisions use Rules until the 1st.
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
            <NumField label="Fee per side (%)" value={draft?.paper.fee_pct} onChange={(v) => setPaper("fee_pct", v)} />
            <NumField label="Slippage (%)" value={draft?.paper.slippage_pct} onChange={(v) => setPaper("slippage_pct", v)} />
          </div>
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
  pending,
  onToggle,
}: {
  market: MarketOut;
  pending: boolean;
  onToggle: (active: boolean) => void;
}) {
  const status = market.active
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
      <td style={{ color: "var(--text-soft)" }}>{source.purpose}</td>
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

import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type {
  EntryPause,
  MarketOut,
  NotificationSettings,
  SourceOut,
} from "../../api/client";
import {
  useCreateShadow,
  useDeleteData,
  useMarkets,
  useNotifications,
  usePrompts,
  usePromoteShadow,
  useRemoveSecret,
  useResetPaper,
  useSaveSecret,
  useSaveSmtpPassword,
  useSaveSmtpSettings,
  useSettingsHistory,
  useShadows,
  useSnapshot,
  useSources,
  useStopShadow,
  useTestNotifications,
  useTestSecret,
  useUpdateMarket,
  useUpdateSource,
} from "../../api/hooks";
import { ApiError } from "../../api/client";
import { Button } from "../../components/Button";
import { Chip } from "../../components/Chip";
import { DataTable } from "../../components/DataTable";
import { Dialog } from "../../components/Dialog";
import { FormField, Input, Select } from "../../components/FormField";
import { PauseSelect } from "../../components/PauseSelect";
import { SegmentedControl } from "../../components/SegmentedControl";
import {
  formatDateTime,
  formatEur,
  formatEurCompact,
  formatPct,
  formatSigned,
  formatTime,
  parseNumberInput,
} from "../../lib/format";
import type { Ctx } from "./controls";
import { formatLeaf, getPath } from "./model";

/** Confirm-retry wrapper for the 409 "looser needs confirm" answers the
 * source/market/SMTP endpoints give. */
function useConfirmRetry() {
  const [pending, setPending] = useState<{
    message: string;
    changes: { key: string; old?: unknown; new?: unknown; direction?: string }[];
    run: (confirm: boolean) => void;
  } | null>(null);
  const ask = (
    err: unknown,
    run: (confirm: boolean) => void,
  ): boolean => {
    if (err instanceof ApiError && err.status === 409) {
      setPending({
        message: err.message,
        changes: err.changes ?? [],
        run,
      });
      return true;
    }
    return false;
  };
  const dialog = pending && (
    <Dialog
      title="This loosens a limit"
      onClose={() => setPending(null)}
      actions={[
        { label: "Cancel", onClick: () => setPending(null) },
        {
          label: "Save anyway",
          variant: "primary",
          onClick: () => {
            pending.run(true);
            setPending(null);
          },
        },
      ]}
    >
      <p>{pending.message}</p>
      <ul className="set-confirm-list">
        {pending.changes.map((c) => (
          <li key={c.key}>
            {c.key}: {formatLeaf(c.old)} → {formatLeaf(c.new)}{" "}
            <Chip variant={c.direction === "looser" ? "amber" : "neutral"}>
              {c.direction ?? "neutral"}
            </Chip>
          </li>
        ))}
      </ul>
    </Dialog>
  );
  return { ask, dialog };
}

// ------------------------------------------------------------- Markets --

export function MarketsBlock() {
  const { data: markets } = useMarkets();
  const { data: snapshot } = useSnapshot();
  const updateMarket = useUpdateMarket();
  const [marketQuery, setMarketQuery] = useState("");
  const [marketError, setMarketError] = useState<string | null>(null);
  const confirm = useConfirmRetry();

  const filtered = useMemo(
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

  const toggle = (m: MarketOut, active: boolean, confirmFlag = false) => {
    setMarketError(null);
    updateMarket.mutate(
      { market: m.market, active, confirm: confirmFlag },
      {
        onError: (e) => {
          if (e instanceof ApiError && e.status === 409) {
            confirm.ask(e, (c) => toggle(m, active, c));
            return;
          }
          setMarketError(e.message);
        },
      },
    );
  };

  return (
    <div className="set-block">
      <Input
        type="search"
        placeholder="Search Bitvavo EUR markets, e.g. HYPE"
        aria-label="Search markets"
        value={marketQuery}
        onChange={(e) => setMarketQuery(e.target.value)}
      />
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
        {filtered.map((m) => (
          <MarketRow
            key={m.market}
            market={m}
            pause={pausedMap.get(m.market) ?? pausedMap.get("*")}
            pending={updateMarket.isPending}
            onToggle={(active) => toggle(m, active)}
          />
        ))}
      </DataTable>
      {confirm.dialog}
      <p className="card__meta" style={{ margin: 0 }}>
        Until the Markets screen lands, the market table stays here under the
        thresholds above.
      </p>
    </div>
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

// ------------------------------------------------------------- Sources --

export function SourceRow({ source }: { source: SourceOut }) {
  const updateSource = useUpdateSource();
  const saveSecret = useSaveSecret();
  const removeSecret = useRemoveSecret();
  const testSecret = useTestSecret();
  const [editing, setEditing] = useState(false);
  const [key, setKey] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const confirm = useConfirmRetry();

  const needsKey = source.key_requirement !== "none";
  const status = !source.enabled
    ? { text: "disabled", variant: "neutral" as const }
    : source.last_error
      ? { text: "failing", variant: "down" as const }
      : source.key_requirement === "required" && !source.key_set
        ? { text: "key missing", variant: "amber" as const }
        : { text: "OK", variant: "up" as const };

  const toggle = (enabled: boolean, confirmFlag = false) =>
    updateSource.mutate(
      { id: source.id, enabled, confirm: confirmFlag },
      {
        onError: (e) => {
          if (confirm.ask(e, (c) => toggle(enabled, c))) return;
          setMessage(e.message);
        },
      },
    );

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
    <>
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
            onClick={() => toggle(!source.enabled)}
          />
        </td>
      </tr>
      {confirm.dialog}
    </>
  );
}

export function SourcesBlock() {
  const { data: sources } = useSources();
  return (
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
  );
}

// ----------------------------------------------------------------- SMTP --

export function SmtpBlock({ ctx }: { ctx: Ctx }) {
  const { data: notif } = useNotifications();
  const saveSmtp = useSaveSmtpSettings();
  const savePassword = useSaveSmtpPassword();
  const testMail = useTestNotifications();
  const [smtpPassword, setSmtpPassword] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const confirm = useConfirmRetry();

  // SMTP lives outside the settings draft: it saves on "Test and save".
  type Smtp = NotificationSettings["smtp"];
  const smtp: Smtp = notif?.smtp ?? ctx.draft.notifications.smtp;
  const [local, setLocal] = useState<Smtp | null>(null);
  useEffect(() => {
    if (notif && local === null) setLocal({ ...notif.smtp });
  }, [notif, local]);
  const setSmtp = (patch: Partial<Smtp>) => {
    setLocal((l) => ({ ...(l ?? smtp), ...patch }));
  };
  const cur = local ?? smtp;

  const save = (confirmFlag = false) =>
    saveSmtp.mutate(
      { smtp: cur, confirm: confirmFlag },
      {
        onSuccess: () => {
          setMessage("Email settings saved as a new version");
          setLocal(null);
        },
        onError: (e) => {
          if (confirm.ask(e, () => save(true))) return;
          setMessage(e.message);
        },
      },
    );

  return (
    <div className="set-block">
      <div className="settings-fields">
        <FormField label="Server host">
          <Input value={cur.host} onChange={(e) => setSmtp({ host: e.target.value })} />
        </FormField>
        <NumFieldLocal
          label="Port"
          value={cur.port}
          int
          onChange={(v) => setSmtp({ port: v })}
        />
        <FormField label="TLS (STARTTLS)">
          <label className="check-label">
            <input
              type="checkbox"
              checked={cur.tls}
              onChange={(e) => setSmtp({ tls: e.target.checked })}
            />
            <span>TLS required</span>
          </label>
        </FormField>
        <FormField label="Username">
          <Input value={cur.username} onChange={(e) => setSmtp({ username: e.target.value })} />
        </FormField>
        <FormField label="Sender">
          <Input value={cur.sender} onChange={(e) => setSmtp({ sender: e.target.value })} />
        </FormField>
        <FormField label="Recipient">
          <Input
            value={cur.recipient}
            onChange={(e) => setSmtp({ recipient: e.target.value })}
          />
        </FormField>
        <FormField label="SMTP password">
          <span style={{ display: "flex", gap: 8 }}>
            <Input
              type="password"
              value={smtpPassword}
              placeholder="Stored in OS keyring, never shown"
              onChange={(e) => setSmtpPassword(e.target.value)}
            />
            <Button
              variant="secondary"
              disabled={!smtpPassword.trim() || savePassword.isPending}
              onClick={() =>
                savePassword.mutate(smtpPassword, {
                  onSuccess: () => setSmtpPassword(""),
                })
              }
            >
              Save
            </Button>
          </span>
        </FormField>
      </div>
      <div className="settings-row">
        <Button
          variant="primary"
          disabled={saveSmtp.isPending}
          onClick={() => save(false)}
        >
          {saveSmtp.isPending ? "Saving…" : "Test and save"}
        </Button>
        <Button
          variant="secondary"
          disabled={testMail.isPending}
          onClick={() => testMail.mutate()}
        >
          {testMail.isPending ? "Sending…" : "Send test"}
        </Button>
        {message && (
          <span className="card__meta" role="status">
            {message}
          </span>
        )}
        {testMail.data && (
          <span className="card__meta" role="status">
            Test sent{testMail.data.browser ? " · browser" : ""}
            {testMail.data.email ? " · email" : ""}
          </span>
        )}
      </div>
      <p className="card__meta" style={{ margin: 0 }}>
        TLS is required. The password lives in the OS keyring and is never
        returned. Email settings save on their own, apart from the
        unsaved-changes bar.
      </p>
      {confirm.dialog}
    </div>
  );
}

function NumFieldLocal({
  label,
  value,
  onChange,
  int = false,
}: {
  label: string;
  value: string | number | undefined;
  onChange: (v: number) => void;
  int?: boolean;
}) {
  return (
    <FormField label={label}>
      <Input
        mono
        type="number"
        step={int ? 1 : "any"}
        value={value ?? ""}
        onChange={(e) => {
          const p = parseNumberInput(e.target.value);
          if (p !== undefined) onChange(p);
        }}
      />
    </FormField>
  );
}

// -------------------------------------------------------------- Shadows --

export function ShadowsBlock({ ctx }: { ctx: Ctx }) {
  const { data: shadows } = useShadows();
  const { data: prompts } = usePrompts();
  const createShadow = useCreateShadow();
  const stopShadow = useStopShadow();
  const promoteShadowMut = usePromoteShadow();
  const [shadowForm, setShadowForm] = useState(false);
  const [shadowModel, setShadowModel] = useState("");
  const [shadowPrompt, setShadowPrompt] = useState("decision.v1");
  const [shadowEntryOrders, setShadowEntryOrders] = useState("market");
  const [promoteId, setPromoteId] = useState<string | null>(null);
  const [shadowError, setShadowError] = useState<string | null>(null);

  return (
    <div className="set-block">
      <div className="settings-row" style={{ justifyContent: "flex-end" }}>
        <Button
          size="sm"
          disabled={(shadows?.items.filter((s) => s.status === "running").length ?? 0) >= 2}
          onClick={() => {
            setShadowModel(String(getPath(ctx.draft, "model.model_id") ?? ""));
            setShadowPrompt(String(getPath(ctx.draft, "model.prompt_version") ?? "decision.v1"));
            setShadowEntryOrders("market");
            setShadowError(null);
            setShadowForm(true);
          }}
        >
          Add shadow (
          {shadows?.items.filter((s) => s.status === "running").length ?? 0} of 2)
        </Button>
      </div>
      <p className="card__meta" style={{ margin: 0 }}>
        A shadow runs the same cycles on its own paper account with another
        model, prompt or entry order type. It never places orders. At most 2.
        Promote opens Settings with the shadow&apos;s differences as unsaved
        changes, saved with a reason like any change.
      </p>
      {(shadows?.items ?? []).map((s) => (
        <div key={s.id} className="shadow-row">
          <div className="shadow-row__main">
            <span className="shadow-row__name">
              {s.name}
              {s.entry_orders === "limit" && <Chip variant="side">v0.2</Chip>}
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
            {" vs live "}
            {s.live_return_pct != null ? `${formatSigned(s.live_return_pct)}%` : "—"}
            {" · "}
            {s.decisions} trades
            {s.entry_orders === "limit" && ` · ${s.orders_filled}/${s.orders_placed} filled`}
          </div>
          <div className="shadow-row__actions">
            <Link to="/inspector" className="btn btn--secondary btn--sm">
              Compare
            </Link>
            {s.status === "running" && (
              <>
                <Button size="sm" onClick={() => setPromoteId(s.id)}>
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
                      setShadowError(e instanceof Error ? e.message : String(e)),
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
      {promoteId && (
        <Dialog
          danger
          title="Promote this shadow?"
          confirmText="PROMOTE"
          onClose={() => setPromoteId(null)}
          actions={[
            { label: "Cancel", onClick: () => setPromoteId(null) },
            {
              label: "Promote",
              variant: "danger-solid",
              requiresConfirm: true,
              onClick: () =>
                promoteShadowMut.mutate(promoteId, {
                  onSettled: () => setPromoteId(null),
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
    </div>
  );
}

// ------------------------------------------------------------ Schedule --

export function ScheduleBlock({ ctx }: { ctx: Ctx }) {
  const rows: [string, string, string][] = [
    [
      "Decision cycles",
      `every 15 min, ${formatLeaf(getPath(ctx.draft, "cycle_offset_s"), "s")} after the quarter hour`,
      "execution · cycle_offset_s",
    ],
    ["Exit watcher", "every 30 s, also while paused", "fixed"],
    ["Market check", "daily 01:00 UTC (02:00 Amsterdam)", "fixed"],
    [
      "Morning briefing",
      `daily ${formatLeaf(getPath(ctx.draft, "learning.briefing_time"), "Amsterdam")}`,
      "model · learning.briefing_time",
    ],
    [
      "Weekly review",
      formatLeaf(getPath(ctx.draft, "learning.weekly_review"), null),
      "model · learning.weekly_review",
    ],
    ["Database backup", "daily 03:00, last 14 kept", "fixed"],
    ["Log clean-up", "daily 03:30", "logs"],
  ];
  return (
    <table className="set-table">
      <thead>
        <tr>
          <th>Job</th>
          <th>When</th>
          <th>Set in</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([job, when, where]) => (
          <tr key={job}>
            <td>{job}</td>
            <td>{when}</td>
            <td className="card__meta">{where}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ------------------------------------------------------------- History --

export function HistoryBlock() {
  const history = useSettingsHistory();
  const pages = history.data?.pages ?? [];
  const versions = pages.flatMap((p) => p.versions);
  const rows = pages.flatMap((p) => p.rows);
  const current = versions[0]?.version;
  return (
    <div className="set-block">
      <p className="card__meta" style={{ margin: 0 }}>
        {current != null ? `current: v${current} · ` : ""}
        Every saved change is a version with its reason. Promote, What-if
        apply, market and source changes save one too.
      </p>
      <table className="set-table">
        <thead>
          <tr>
            <th>Version</th>
            <th>When</th>
            <th>Change</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => (
            <tr key={v.version}>
              <td className="num">v{v.version}</td>
              <td>{formatDateTime(v.at)}</td>
              <td>
                {v.changes.length === 0
                  ? "—"
                  : v.changes.map((c) => (
                      <div key={c.key} className="set-history-change">
                        <span>{c.key}</span>{" "}
                        <span className="num">
                          {formatLeaf(c.old)} → {formatLeaf(c.new)}
                        </span>{" "}
                        <Chip
                          variant={c.direction === "looser" ? "amber" : "neutral"}
                        >
                          {c.direction}
                        </Chip>
                      </div>
                    ))}
              </td>
              <td>{v.reason}</td>
            </tr>
          ))}
          {rows.map((r, i) => (
            <tr key={`r${i}`}>
              <td className="num card__meta">old</td>
              <td>{formatDateTime(r.at)}</td>
              <td>
                <div className="set-history-change">
                  <span>{r.key}</span>{" "}
                  <span className="num">
                    {formatLeaf(r.old)} → {formatLeaf(r.new)}
                  </span>
                </div>
              </td>
              <td className="card__meta">before versions</td>
            </tr>
          ))}
        </tbody>
      </table>
      {history.hasNextPage && (
        <Button
          variant="secondary"
          disabled={history.isFetchingNextPage}
          onClick={() => void history.fetchNextPage()}
        >
          {history.isFetchingNextPage ? "Loading…" : "Show earlier"}
        </Button>
      )}
    </div>
  );
}

// ----------------------------------------------------------- Paper acct --

export function PaperBlock({ ctx }: { ctx: Ctx }) {
  const { data: snapshot } = useSnapshot();
  const reset = useResetPaper();
  const deleteData = useDeleteData();
  const [resetOpen, setResetOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  void ctx;

  return (
    <div className="set-block">
      <div className="kv-box">
        <div className="kv">
          <span>Current account</span>
          <span>
            #{snapshot?.account_id ?? "—"} · equity{" "}
            {snapshot ? formatEur(snapshot.equity) : "—"}
          </span>
        </div>
        <div className="kv">
          <span>Model call records</span>
          <span>
            Every call is kept with its exact input and output
            {snapshot ? ` · ${snapshot.usage.calls_today} calls today` : ""}
          </span>
        </div>
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
      <p className="card__meta" style={{ margin: 0 }}>
        Reset closes every position, clears cash, trades and the Killed state,
        and starts the next account. Decisions and model calls stay. Delete
        all data removes everything except settings and keys.
      </p>
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
                reset.mutate(undefined, { onSettled: () => setResetOpen(false) }),
            },
          ]}
        >
          <p>
            Closes all positions and starts paper account #
            {(snapshot?.account_id ?? 0) + 1}. Decisions, LLM calls, verdicts
            and outcomes are kept; trades, the equity curve, Performance and
            Readiness start fresh. Export history first if you want it.
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
                deleteData.mutate(undefined, { onSettled: () => setDeleteOpen(false) }),
            },
          ]}
        >
          <p>
            Deletes every account, trade, decision, model call, verdict,
            outcome, briefing and Ask conversation. Settings and API keys
            stay. This cannot be undone.
          </p>
        </Dialog>
      )}
    </div>
  );
}

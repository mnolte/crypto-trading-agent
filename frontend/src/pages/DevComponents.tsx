import { useState, type ReactNode } from "react";
import type { UTCTimestamp } from "lightweight-charts";
import { ActionBadge } from "../components/ActionBadge";
import { AppShell } from "../components/AppShell";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { CandleChart, Sparkline, type Candle } from "../components/Charts";
import { Chip, RegimeChip } from "../components/Chip";
import { DataTable } from "../components/DataTable";
import { Dialog } from "../components/Dialog";
import { EmptyState } from "../components/EmptyState";
import { ErrorBadge } from "../components/ErrorBadge";
import { CheckField, FormField, Input, Select } from "../components/FormField";
import { GateList } from "../components/GateList";
import { GlossaryTerm } from "../components/GlossaryTerm";
import { KpiTile } from "../components/KpiTile";
import { Meter } from "../components/Meter";
import { PairTabs, type PairTabItem } from "../components/PairTabs";
import { SegmentedControl } from "../components/SegmentedControl";
import { SignalChip } from "../components/SignalChip";
import { Drawer, SidePanel } from "../components/SlidePanel";
import { StatusPill } from "../components/StatusPill";
import { formatEur, formatSignedEur } from "../lib/format";

const PAIRS: PairTabItem[] = [
  { id: "BTC-EUR", chg24h: 1.42 },
  { id: "ETH-EUR", chg24h: -0.85 },
  { id: "SOL-EUR", chg24h: 3.1 },
  { id: "XRP-EUR", chg24h: 0.02 },
  { id: "ADA-EUR", chg24h: -2.4, paused: "18h" },
  { id: "LINK-EUR", chg24h: 0.9 },
  { id: "AVAX-EUR", chg24h: 0, stale: true },
  { id: "DOGE-EUR", chg24h: -1.1 },
  { id: "LTC-EUR", chg24h: 0.4 },
];

function candles(): Candle[] {
  const out: Candle[] = [];
  let close = 61_000;
  const start = 1_727_000_000;
  for (let i = 0; i < 72; i += 1) {
    const drift = Math.sin(i / 6) * 120 + (i % 7 === 0 ? 160 : -40);
    const open = close;
    close = Math.max(1_000, close + drift);
    out.push({
      time: (start + i * 3600) as UTCTimestamp,
      open,
      high: Math.max(open, close) + 60,
      low: Math.min(open, close) - 60,
      close,
    });
  }
  return out;
}

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="dev-section">
      <div className="dev-label">{label}</div>
      {children}
    </div>
  );
}

export function DevComponents() {
  const [tf, setTf] = useState("1h");
  const [period, setPeriod] = useState("1M");
  const [pair, setPair] = useState("BTC-EUR");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);

  return (
    <AppShell title="Components" controls={false}>
      <Section label="Button">
        <div className="dev-row">
          <Button variant="primary">Run now</Button>
          <Button variant="secondary">Details</Button>
          <Button variant="ghost">Cancel</Button>
          <Button variant="danger">Stop &amp; flatten</Button>
          <Button variant="danger-outline">Reset paper account</Button>
          <Button variant="danger-solid">Delete all data</Button>
        </div>
        <div className="dev-row">
          <Button variant="primary" disabled>
            Primary disabled
          </Button>
          <Button variant="secondary" size="sm">
            Small
          </Button>
          <Button variant="secondary" size="xs">
            Chip-sized
          </Button>
          <Button
            variant="secondary"
            icon={
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <polyline points="20 6 20 11 15 11" />
                <path d="M20 11a8 8 0 1 0-2.3 5.7" />
              </svg>
            }
          >
            Icon + label
          </Button>
        </div>
      </Section>

      <Section label="StatusPill · ErrorBadge">
        <div className="dev-row">
          <StatusPill status="running" />
          <StatusPill status="paused" />
          <StatusPill status="killed" />
          <StatusPill status="error" />
          <StatusPill
            status="disconnected"
            lastKnown={{ status: "running", at: Date.UTC(2024, 9, 9, 10, 32, 0) }}
          />
          <ErrorBadge count={2} />
          <ErrorBadge count={1} />
        </div>
      </Section>

      <Section label="SegmentedControl">
        <div className="dev-row">
          <SegmentedControl
            label="Trading mode"
            value="paper"
            options={[
              { value: "paper", label: "Paper" },
              {
                value: "live",
                label: "Live",
                disabled: true,
                title: "Live trading comes after the paper phase",
              },
            ]}
          />
          <SegmentedControl
            label="Timeframe"
            flat
            value={tf}
            onChange={setTf}
            options={[
              { value: "1h", label: "1h" },
              { value: "4h", label: "4h" },
              { value: "1d", label: "1d" },
            ]}
          />
          <SegmentedControl
            label="Period"
            flat
            value={period}
            onChange={setPeriod}
            options={[
              { value: "1W", label: "1W" },
              { value: "1M", label: "1M" },
              { value: "3M", label: "3M" },
              { value: "1Y", label: "1Y" },
            ]}
          />
        </div>
      </Section>

      <Section label="PairTabs + More menu (searchable, pause badge, stale)">
        <PairTabs pairs={PAIRS} value={pair} onChange={setPair} />
      </Section>

      <Section label="Card">
        <div className="dev-row" style={{ alignItems: "stretch" }}>
          <Card title="Eyebrow heading" style={{ flex: "1 1 260px" }}>
            Panel body.
          </Card>
          <Card
            title="Section heading"
            heading="section"
            style={{ flex: "1 1 260px" }}
            actions={<Button size="sm">Action</Button>}
          >
            With a header action.
          </Card>
        </div>
        <div className="dev-row" style={{ alignItems: "stretch" }}>
          <Card title="Accent" variant="accent" style={{ flex: "1 1 200px" }}>
            Accent border.
          </Card>
          <Card title="Accent dim" variant="accent-dim" style={{ flex: "1 1 200px" }}>
            Dim accent border.
          </Card>
          <Card title="Danger" variant="danger" style={{ flex: "1 1 200px" }}>
            Danger border.
          </Card>
        </div>
      </Section>

      <Section label="KpiTile">
        <div className="dev-row" style={{ alignItems: "stretch" }}>
          <Card style={{ flex: "1 1 200px", padding: "var(--space-4)" }}>
            <KpiTile
              label="Equity"
              value={formatEur("10412.80")}
              sub="+4.13% since start"
              subTone="up"
            />
          </Card>
          <Card style={{ flex: "1 1 200px", padding: "var(--space-4)" }}>
            <KpiTile
              label="Today"
              value={formatSignedEur("86.20")}
              sub="+0.83% · 2 trades"
              subTone="up"
            />
          </Card>
          <Card style={{ flex: "1 1 200px", padding: "var(--space-4)" }}>
            <KpiTile
              label="Drawdown"
              value="−2.3%"
              sub="of −8% kill"
              subTone="down"
              meter={{ pct: 29, color: "accent" }}
              glossary="How far equity is below its highest point so far. At −8% the kill switch closes everything."
            />
          </Card>
          <Card style={{ flex: "1 1 200px", padding: "var(--space-4)" }}>
            <KpiTile label="Next cycle" value="11:41" sub="every 15 min · cycle #728" />
          </Card>
        </div>
      </Section>

      <Section label="Meter">
        <Card style={{ padding: "var(--space-4)" }}>
          <div className="dev-section">
            <Meter pct={48} />
            <Meter pct={30} color="up" />
            <Meter pct={78} color="down" />
            <Meter pct={60} color="muted" />
            <Meter pct={21} large />
            <Meter
              segments={[
                { pct: 14, color: "accent" },
                { pct: 7, color: "up" },
              ]}
            />
          </div>
        </Card>
      </Section>

      <Section label="ActionBadge">
        <div className="dev-row">
          <ActionBadge action="BUY" />
          <ActionBadge action="SELL" />
          <ActionBadge action="HOLD" />
          <ActionBadge action="SKIP" />
          <ActionBadge action="BUY" small />
          <ActionBadge action="SELL" small />
          <ActionBadge action="HOLD" small />
          <ActionBadge action="SKIP" small />
        </div>
      </Section>

      <Section label="Chip · SignalChip · RegimeChip">
        <div className="dev-row">
          <Chip>neutral</Chip>
          <Chip variant="up">up</Chip>
          <Chip variant="down">down</Chip>
          <Chip variant="amber">amber</Chip>
          <Chip variant="side">sideways</Chip>
        </div>
        <div className="dev-row">
          <SignalChip signal={{ name: "RSI 58", tf: "1h", read: "bullish" }} />
          <SignalChip signal={{ name: "OBV ↓", tf: "1h", read: "bearish" }} />
          <SignalChip signal={{ name: "ADX 27", tf: "1h", read: "neutral" }} />
        </div>
        <div className="dev-row">
          <RegimeChip trend="up" volatility="normal" age4h={14} label="regime" />
          <RegimeChip trend="down" volatility="high" age4h={8} />
          <RegimeChip trend="sideways" volatility="high" age4h={3} />
          <Chip pill>warming up, no regime yet</Chip>
        </div>
      </Section>

      <Section label="GateList">
        <Card title="Safety gates" style={{ padding: "var(--space-4)" }}>
          <GateList
            gates={[
              { name: "Risk / reward ≥ 2.0", status: "pass", value: "2.1" },
              { name: "Costs ≤ 30% of risk", status: "pass", value: "29%" },
              { name: "Total exposure ≤ 100%", status: "warn", value: "94% after" },
              { name: "Daily loss limit", status: "fail", value: "−3.1% today" },
              { name: "Shock brake", status: "pass", value: "off" },
            ]}
          />
        </Card>
      </Section>

      <Section label="DataTable">
        <Card style={{ padding: 0 }}>
          <DataTable
            columns={[
              { key: "pair", label: "Pair" },
              { key: "qty", label: "Qty", numeric: true },
              { key: "entry", label: "Entry", numeric: true },
              { key: "pnl", label: "P&L", numeric: true },
            ]}
          >
            <tr>
              <td className="text text--strong">BTC-EUR</td>
              <td className="num">0.0807</td>
              <td className="num">€61,240.50</td>
              <td className="num" style={{ color: "var(--up)" }}>
                {formatSignedEur("86.20")}
              </td>
            </tr>
            <tr>
              <td className="text text--strong">SOL-EUR</td>
              <td className="num">12.4</td>
              <td className="num">€141.02</td>
              <td className="num" style={{ color: "var(--down)" }}>
                {formatSignedEur("-12.40")}
              </td>
            </tr>
          </DataTable>
        </Card>
      </Section>

      <Section label="Banner">
        <Banner variant="info">
          Bitvavo keys are read-only; the app can see prices and your balance, never
          trade.
        </Banner>
        <Banner
          variant="warning"
          actions={<Button size="sm" variant="ghost">Raise the cap</Button>}
        >
          Model cost cap reached: €10.00 of €10 this month. Decisions now use the Rules
          baseline until 1 Nov.
        </Banner>
        <Banner variant="danger">
          Stale data: no Bitvavo answer for 3 minutes. The agent holds; nothing was
          bought or sold.
        </Banner>
      </Section>

      <Section label="FormField">
        <div className="dev-row" style={{ alignItems: "flex-start" }}>
          <div style={{ flex: "1 1 220px" }}>
            <FormField label="Risk per trade" unit="% of equity">
              <Input mono defaultValue="1.0" />
            </FormField>
          </div>
          <div style={{ flex: "1 1 220px" }}>
            <FormField
              label="Monthly model cost cap"
              unit="€ / month"
              error="Between €1 and €100."
            >
              <Input mono invalid defaultValue="0" />
            </FormField>
          </div>
          <div style={{ flex: "1 1 220px" }}>
            <FormField label="Decider">
              <Select defaultValue="openai">
                <option value="openai">OpenAI model</option>
                <option value="rules">Rules baseline</option>
              </Select>
            </FormField>
          </div>
        </div>
        <CheckField
          label="Plain-language explanations"
          sub="Dotted-underline terms get a tooltip that explains the jargon."
          defaultChecked
        />
      </Section>

      <Section label="GlossaryTerm">
        <Card style={{ padding: "var(--space-4)", fontSize: "var(--fs-small)" }}>
          <GlossaryTerm tip="What you would lose if every open position hit its stop at the same time.">
            Open risk
          </GlossaryTerm>{" "}
          is 1.2% of the 2.5% cap;{" "}
          <GlossaryTerm tip="Average True Range — how much the price typically moves in one bar.">
            ATR
          </GlossaryTerm>{" "}
          sizes the stop.
        </Card>
      </Section>

      <Section label="EmptyState">
        <Card style={{ padding: "var(--space-4)" }}>
          <EmptyState
            title="No insights yet"
            footer="Insights appear after about 30 closed trades, so the stats mean something."
          />
        </Card>
      </Section>

      <Section label="Charts — CandleChart (lightweight-charts) · Sparkline (SVG)">
        <Card title="BTC-EUR · 1h" style={{ padding: "var(--space-4)" }}>
          <CandleChart candles={candles()} height={280} />
        </Card>
        <Card title="Sparkline" style={{ padding: "var(--space-4)" }}>
          <Sparkline points={[42, 45, 44, 50, 49, 55, 58, 54, 60, 62, 59, 64]} height={120} />
        </Card>
      </Section>

      <Section label="Dialog (opens centred; Escape closes)">
        <div className="dev-row">
          <Button variant="secondary" onClick={() => setDialogOpen(true)}>
            Open dialog
          </Button>
          <Button variant="danger-outline" onClick={() => setConfirmOpen(true)}>
            Open typed-confirmation dialog
          </Button>
        </div>
        <div className="dev-frame" style={{ height: 340 }}>
          <Dialog
            title="Stop and flatten?"
            danger
            onClose={() => {}}
            note="Shown inline here; in the app it opens over the dimmed screen."
            actions={[
              { label: "Cancel", variant: "secondary" },
              { label: "Stop & flatten", variant: "danger-solid" },
            ]}
          >
            <p style={{ margin: 0 }}>
              Closes all 3 open positions at market and pauses the agent. Positions
              close at the current price; fees apply.
            </p>
          </Dialog>
        </div>
      </Section>

      <Section label="Drawer (560 px) · SidePanel (520 px)">
        <div className="dev-row">
          <Button variant="secondary" onClick={() => setDrawerOpen(true)}>
            Open decision drawer
          </Button>
          <Button variant="secondary" onClick={() => setPanelOpen(true)}>
            Open Ask panel
          </Button>
        </div>
        <div className="dev-frame">
          <Drawer
            label="BTC-EUR decision"
            title="BTC-EUR decision"
            meta="Cycle #728 · 12:30 CEST · gpt-4o-mini · prompt v1"
            onClose={() => {}}
          >
            <EmptyState title="Decision detail" footer="Frame only — contents land with the drawer build step." />
          </Drawer>
        </div>
      </Section>

      {dialogOpen && (
        <Dialog
          title="Veto this trade?"
          onClose={() => setDialogOpen(false)}
          actions={[
            { label: "Keep the trade", variant: "secondary", onClick: () => setDialogOpen(false) },
            { label: "Veto", variant: "primary", onClick: () => setDialogOpen(false) },
          ]}
        >
          <p style={{ margin: 0 }}>
            The pending BUY on {pair} expires without filling. The agent logs the veto
            and moves on.
          </p>
        </Dialog>
      )}

      {confirmOpen && (
        <Dialog
          title="Delete all data?"
          danger
          confirmText="DELETE"
          onClose={() => setConfirmOpen(false)}
          note="Settings and API keys stay. This cannot be undone; the last daily backup is from 03:00."
          actions={[
            { label: "Cancel", variant: "secondary", onClick: () => setConfirmOpen(false) },
            {
              label: "Delete all data",
              variant: "danger-solid",
              requiresConfirm: true,
              onClick: () => setConfirmOpen(false),
            },
          ]}
        >
          <p style={{ margin: 0 }}>
            Deletes every account, trade, decision, model call, verdict, outcome,
            briefing and Ask conversation.
          </p>
        </Dialog>
      )}

      {drawerOpen && (
        <Drawer
          label="BTC-EUR decision"
          title="BTC-EUR decision"
          meta="Cycle #728 · 12:30 CEST · gpt-4o-mini · prompt v1"
          onClose={() => setDrawerOpen(false)}
        >
          <EmptyState title="Decision detail" footer="Frame only — contents land with the drawer build step." />
        </Drawer>
      )}

      {panelOpen && (
        <SidePanel
          label="Ask the agent"
          title="Ask the agent"
          onClose={() => setPanelOpen(false)}
        >
          <EmptyState title="Ask the agent" footer="Arrives with the Ask build step." />
        </SidePanel>
      )}
    </AppShell>
  );
}

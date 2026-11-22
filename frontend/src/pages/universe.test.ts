import { describe, expect, it } from "vitest";
import type { MarketStatusOut, UniverseMarketOut } from "../api/client";
import {
  baseSymbol,
  filterMatches,
  foldGroups,
  foldLabel,
  backfillResult,
  formatRunSeconds,
  funnelLabel,
  jobResultText,
  needsLook,
  parseFilter,
  sortMarkets,
  statusChip,
  statusLine,
  syncAttention,
  syncRows,
  syncSummary,
} from "./universe";

function market(
  pair: string,
  kind: MarketStatusOut["kind"],
  over: Partial<UniverseMarketOut> = {},
): UniverseMarketOut {
  return {
    market: pair,
    name: pair.replace("-EUR", ""),
    status: { kind, line: `${kind} line`, held: false, facts: {} },
    eligible: kind === "eligible",
    active: [
      "warming_up",
      "failing",
      "fails",
      "paused",
      "held",
      "active",
    ].includes(kind),
    failed_rule: null,
    below_streak: 0,
    within_streak: 0,
    checked_at: null,
    activated_at: null,
    last_price: "1.5",
    price_at: null,
    volume_30d_eur: "1000000",
    spread_pct: "0.1",
    vs_btc_30d_pct: "1.0",
    corr_btc_30d: "0.5",
    regime: null,
    ...over,
  };
}

describe("parseFilter", () => {
  it("accepts known filters and falls back to all", () => {
    expect(parseFilter("failing")).toBe("failing");
    expect(parseFilter("warming")).toBe("warming");
    expect(parseFilter("bogus")).toBe("all");
    expect(parseFilter(null)).toBe("all");
  });
});

describe("filterMatches", () => {
  const active = market("AAA-EUR", "active", { eligible: true });
  const eligible = market("BBB-EUR", "eligible");
  const excluded = market("CCC-EUR", "excluded", { eligible: false, active: false });
  const recovering = market("DDD-EUR", "recovering", {
    eligible: false,
    active: false,
  });
  const failing = market("EEE-EUR", "failing", { eligible: false });
  const warming = market("FFF-EUR", "warming_up", { eligible: true });

  it("maps each filter to its markets", () => {
    const all = [active, eligible, excluded, recovering, failing, warming];
    expect(all.filter((m) => filterMatches(m, "active"))).toEqual([
      active,
      failing,
      warming,
    ]);
    expect(all.filter((m) => filterMatches(m, "eligible"))).toEqual([
      active,
      eligible,
      warming,
    ]);
    expect(all.filter((m) => filterMatches(m, "failing"))).toEqual([failing]);
    expect(all.filter((m) => filterMatches(m, "warming"))).toEqual([warming]);
    // Excluded = every market that isn't eligible, recovering and the
    // failing active one included.
    expect(all.filter((m) => filterMatches(m, "excluded"))).toEqual([
      excluded,
      recovering,
      failing,
    ]);
    expect(all.filter((m) => filterMatches(m, "all"))).toHaveLength(6);
  });
});

describe("statusChip", () => {
  it("names each kind per the spec", () => {
    expect(statusChip(market("A-EUR", "warming_up")).text).toBe(
      "active · warming up",
    );
    expect(statusChip(market("A-EUR", "failing")).variant).toBe("amber");
    expect(
      statusChip(
        market("A-EUR", "fails", {
          status: {
            kind: "fails",
            line: "",
            held: false,
            facts: { fails: 2, of: 3 },
          },
        }),
      ).text,
    ).toBe("active · 2 of 3 fails");
    expect(statusChip(market("A-EUR", "paused")).text).toBe(
      "active · entries paused",
    );
    expect(statusChip(market("A-EUR", "held")).text).toBe("active · held");
    expect(statusChip(market("A-EUR", "active")).variant).toBe("up");
    expect(statusChip(market("A-EUR", "eligible")).variant).toBe("eligible");
    expect(
      statusChip(
        market("A-EUR", "recovering", {
          status: {
            kind: "recovering",
            line: "",
            held: false,
            facts: { passing: 1, need: 3 },
          },
        }),
      ).text,
    ).toBe("recovering · 1 of 3");
    expect(statusChip(market("A-EUR", "excluded")).variant).toBe("down");
    expect(statusChip(market("A-EUR", "held_not_active")).text).toBe(
      "held · not active",
    );
  });
});

describe("foldGroups", () => {
  function active(i: number, kind: MarketStatusOut["kind"] = "active") {
    return market(`A${String(i).padStart(2, "0")}-EUR`, kind);
  }

  it("caps active at 6, eligible at 2, excluded at 3 and keeps attention", () => {
    const markets = [
      ...Array.from({ length: 8 }, (_, i) => active(i)),
      active(8, "failing"), // attention: always shown
      ...Array.from({ length: 4 }, (_, i) =>
        market(`E${i}-EUR`, "eligible"),
      ),
      ...Array.from({ length: 5 }, (_, i) =>
        market(`X${i}-EUR`, "excluded", { eligible: false, active: false }),
      ),
      market("R-EUR", "recovering", { eligible: false, active: false }),
    ];
    const groups = foldGroups(markets);
    const byGroup = Object.fromEntries(groups.map((g) => [g.group, g]));
    expect(byGroup.active.shown).toHaveLength(7); // 6 + the failing one
    expect(byGroup.active.hidden).toHaveLength(2);
    expect(byGroup.eligible.shown).toHaveLength(2);
    expect(byGroup.eligible.hidden).toHaveLength(2);
    expect(byGroup.excluded.shown).toHaveLength(3);
    expect(byGroup.excluded.hidden).toHaveLength(2);
    expect(byGroup.recovering.shown).toHaveLength(1);
    expect(byGroup.recovering.hidden).toHaveLength(0);
  });

  it("keeps every recovering market visible", () => {
    const recovering = Array.from({ length: 6 }, (_, i) =>
      market(`R${i}-EUR`, "recovering", { eligible: false, active: false }),
    );
    const groups = foldGroups(recovering);
    expect(groups.find((g) => g.group === "recovering")!.shown).toHaveLength(6);
  });
});

describe("foldLabel", () => {
  it("names hidden markets for small sets only", () => {
    const hidden = [market("ADA-EUR", "active"), market("LINK-EUR", "active")];
    expect(foldLabel("active", hidden)).toBe("2 more active: ADA, LINK");
    const many = Array.from({ length: 20 }, (_, i) =>
      market(`M${i}-EUR`, "excluded", { eligible: false, active: false }),
    );
    expect(foldLabel("excluded", many)).toBe("20 more excluded");
  });
});

describe("needsLook", () => {
  it("flags active markets that need a look", () => {
    expect(needsLook(market("A-EUR", "failing"))).toBe(true);
    expect(needsLook(market("A-EUR", "warming_up"))).toBe(true);
    expect(needsLook(market("A-EUR", "paused"))).toBe(true);
    expect(needsLook(market("A-EUR", "held"))).toBe(true);
    expect(needsLook(market("A-EUR", "active"))).toBe(false);
    expect(needsLook(market("A-EUR", "eligible"))).toBe(false);
  });
});

describe("sortMarkets", () => {
  it("sorts by volume descending and keeps nulls last", () => {
    const rows = [
      market("A-EUR", "active", { volume_30d_eur: "5000000" }),
      market("B-EUR", "active", { volume_30d_eur: null }),
      market("C-EUR", "active", { volume_30d_eur: "9000000" }),
    ];
    const sorted = sortMarkets(rows, "volume", "desc");
    expect(sorted.map((m) => m.market)).toEqual(["C-EUR", "A-EUR", "B-EUR"]);
  });

  it("returns the input order when no sort is set", () => {
    const rows = [market("B-EUR", "active"), market("A-EUR", "active")];
    expect(sortMarkets(rows, null, "asc").map((m) => m.market)).toEqual([
      "B-EUR",
      "A-EUR",
    ]);
  });
});

describe("statusLine", () => {
  it("strips the timeframe counts from a warming-up line", () => {
    const m = market("S-EUR", "warming_up", {
      status: {
        kind: "warming_up",
        line: "1h 800 of 800 · 4h 0 of 300 · 1d 300 of 300 · Bitvavo 429 · retry 12:45 · trades once loaded",
        held: false,
        facts: {},
      },
    });
    expect(statusLine(m)).toBe(
      "Bitvavo 429 · retry 12:45 · trades once loaded",
    );
  });

  it("keeps other kinds' lines untouched", () => {
    const m = market("A-EUR", "failing");
    expect(statusLine(m)).toBe("failing line");
  });
});

describe("funnelLabel", () => {
  const thresholds = {
    min_avg_daily_volume_eur: "1000000",
    max_avg_spread_pct: "0.15",
    min_daily_candles: 300,
  };

  it("composes labels with the run's thresholds", () => {
    expect(funnelLabel("listed", null, thresholds)).toBe("Bitvavo EUR markets");
    expect(funnelLabel("volume", null, thresholds)).toBe(
      "30-day average daily volume ≥ €1M",
    );
    expect(funnelLabel("spread", null, thresholds)).toBe(
      "average spread over 24h ≤ 0.15%",
    );
    expect(funnelLabel("history", null, thresholds)).toBe("300 daily candles");
    expect(funnelLabel("all", null, thresholds)).toBe("pass every rule today");
  });
});

describe("sync helpers", () => {
  const okJob = {
    job: "daily_check",
    market: null,
    status: "ok",
    started_at: "2026-01-01T01:00:00Z",
    finished_at: "2026-01-01T01:00:41Z",
    duration_seconds: 41,
    done: 236,
    total: 236,
    failures: [],
    progress: null,
  };

  it("builds the five job rows incl. the watcher and History", () => {
    const rows = syncRows([okJob], 15);
    expect(rows.map((r) => r.label)).toEqual([
      "Exchange rules",
      "Daily check",
      "Candles",
      "Prices for stops",
      "History for activated markets",
    ]);
    expect(rows[3].job).toBeNull();
    expect(rows[4].backfills).toEqual([]);
  });

  it("groups backfills into the one History row, attention first", () => {
    const backfill = {
      ...okJob,
      job: "backfill",
      market: "SUI-EUR",
      status: "running",
      progress: { loaded: 1100, needed: 1400, eta_seconds: 60 },
    };
    const done = {
      ...okJob,
      job: "backfill",
      market: "ADA-EUR",
      status: "ok",
      done: 1400,
      total: 1400,
    };
    const rows = syncRows([okJob, backfill, done], 15);
    const hist = rows.find((r) => r.key === "backfill");
    expect(hist?.label).toBe("History for activated markets");
    expect(hist?.backfills.map((j) => j.market)).toEqual([
      "ADA-EUR",
      "SUI-EUR",
    ]);
    // The running market's segment leads the result text.
    expect(backfillResult(hist!.backfills)).toContain("SUI-EUR · loading 1,100 of 1,400");
    expect(backfillResult(hist!.backfills)).toContain("ADA-EUR · 1,400 of 1,400");
  });

  it("writes the spec's ok-state result lines", () => {
    const totals = {
      eurMarkets: 236,
      eligible: 15,
      failing: 1,
      recovering: 1,
      active: 10,
    };
    expect(jobResultText({ ...okJob, job: "exchange_rules" }, totals)).toBe(
      "236 EUR markets · order sizes and precision",
    );
    expect(jobResultText(okJob, totals)).toBe(
      "236 checked · 15 eligible · 1 failing · 1 recovering",
    );
    expect(jobResultText({ ...okJob, job: "candles" }, totals)).toBe(
      "10 active markets · 1h each cycle, 4h and 1d each hour",
    );
    // Without totals the generic done-of-total fallback stays.
    expect(jobResultText(okJob)).toBe("236 of 236");
  });

  it("counts failed, running and late jobs", () => {
    const failed = {
      ...okJob,
      job: "candles",
      status: "failed",
      failures: [{ what: "x", error: "y" }],
    };
    const rows = syncRows([okJob, failed], 15);
    const attention = syncAttention(rows, Date.parse("2026-01-02T00:00:00Z"));
    // daily_check ok at 01:00 is 23 h old at Jan 2 00:00 — under 26 h.
    expect(attention.failed).toBe(1);
    expect(attention.late).toBe(0);
    const late = syncAttention(rows, Date.parse("2026-01-03T04:00:00Z"));
    expect(late.late).toBe(1);
    expect(syncSummary(attention, rows.length)).toBe("open: 1 failed");
    expect(syncSummary(late, rows.length)).toBe("open: 1 failed · 1 late");
    expect(syncSummary({ failed: 0, running: 0, late: 0 }, 5)).toBe(
      "All 5 jobs ran on time",
    );
  });

  it("formats run durations", () => {
    expect(formatRunSeconds(1.1)).toBe("1.1 s");
    expect(formatRunSeconds(41)).toBe("41 s");
    expect(formatRunSeconds(null)).toBe("—");
  });
});

describe("baseSymbol", () => {
  it("strips the EUR suffix", () => {
    expect(baseSymbol("NEAR-EUR")).toBe("NEAR");
    expect(baseSymbol("BTC-EUR")).toBe("BTC");
  });
});

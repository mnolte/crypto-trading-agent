"""Risk gates: each gate passes and fails at its boundary; sizing never
exceeds risk %, the open-risk cap, exposure or cash. Exit rules: stop,
target, breakeven, trailing, time stop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from app.agent.broker.paper import HeldPosition, PaperBroker
from app.agent.market.base import MarketInfo
from app.agent.risk import BuyContext, check_buy, check_exit, update_trail
from app.db import AccountRow
from app.models import PaperSettings, Settings

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _broker(
    cash: str = "10000",
    positions: dict[str, HeldPosition] | None = None,
    peak: str = "10000",
    day_start: str = "10000",
) -> PaperBroker:
    broker = PaperBroker(
        AccountRow(
            id=1,
            started_at=NOW,
            start_balance="10000",
            cash=cash,
            peak_equity=peak,
            day_start_equity=day_start,
            day_start_date="2026-10-11",
            active=True,
        ),
        PaperSettings(),
    )
    broker.positions.update(positions or {})
    return broker


def _ctx(**kw: object) -> BuyContext:
    base = {
        "pair": "X-EUR",
        "now": NOW,
        "entry": Decimal("100"),
        "stop": Decimal("96"),  # 4% away → clears the 2% fee gate
        "target": Decimal("110"),  # R/R 2.5 on signal, ~2.46 on the fill
        "atr_1h": Decimal("1.5"),
        "has_position": False,
        "last_stop_out_at": None,
    }
    base.update(kw)
    return BuyContext(**base)  # type: ignore[arg-type]


def _settings() -> Settings:
    return Settings()


# ------------------------------------------------------------------- gates --


def test_min_rr_boundary() -> None:
    ok = check_buy(_ctx(), _broker(), None, _settings())
    assert ok.outcome == "fill"
    assert next(g for g in ok.gates if g.name == "Min reward/risk").status == "pass"
    # R/R is measured on the expected fill 100.05: exactly 2.0 is
    # (108.15-100.05)/(100.05-96); just under fails.
    edge = check_buy(_ctx(target=Decimal("108.15")), _broker(), None, _settings())
    assert edge.outcome == "fill"
    bad = check_buy(_ctx(target=Decimal("108.14")), _broker(), None, _settings())
    assert bad.outcome == "skip" and bad.skip_reason == "Min reward/risk"
    assert next(g for g in bad.gates if g.name == "Min reward/risk").status == "fail"


def test_rr_gate_uses_expected_fill_price() -> None:
    """R/R passes on the signal price but fails at the expected fill → SKIP."""
    # Signal R/R: (108.09-100)/(100-96) = 2.02 >= 2.
    # Fill R/R:   (108.09-100.05)/(100.05-96) = 1.985 < 2.
    v = check_buy(_ctx(target=Decimal("108.09")), _broker(), None, _settings())
    assert v.outcome == "skip" and v.skip_reason == "Min reward/risk"
    assert next(g for g in v.gates if g.name == "Min reward/risk").status == "fail"
    # A passing plan records both the signal price and the expected fill.
    ok = check_buy(_ctx(), _broker(), None, _settings())
    assert ok.plan is not None
    assert ok.plan.entry == Decimal("100")
    assert ok.plan.fill_entry == Decimal("100.05")


def test_fee_gate_boundary() -> None:
    # Costs 0.6% of price; the gate needs the stop distance >= 2% of the
    # expected fill: 100.05 x 2% = 2.001 -> stop 98.049 is the boundary.
    ok = check_buy(
        _ctx(stop=Decimal("98.049"), target=Decimal("110")), _broker(), None, _settings()
    )
    assert ok.outcome == "fill"
    tight = check_buy(
        _ctx(stop=Decimal("98.05"), target=Decimal("110")),
        _broker(),
        None,
        _settings(),
    )
    assert tight.outcome == "skip" and tight.skip_reason == "Costs vs risk"


def test_one_position_per_pair() -> None:
    broker = _broker(
        positions={
            "X-EUR": HeldPosition(
                pair="X-EUR",
                qty=Decimal("1"),
                entry=Decimal("100"),
                stop=Decimal("95"),
                target=Decimal("110"),
                opened_at=NOW,
                decision_id="d",
                last=Decimal("100"),
            ),
        }
    )
    v = check_buy(_ctx(has_position=True), broker, None, _settings())
    assert v.outcome == "skip" and v.skip_reason == "One position per pair"


def test_cooldown_after_stopout() -> None:
    v = check_buy(
        _ctx(last_stop_out_at=NOW - timedelta(hours=5, minutes=59)),
        _broker(),
        None,
        _settings(),
    )
    assert v.outcome == "skip" and v.skip_reason == "Cooldown after stop-out"
    ok = check_buy(
        _ctx(last_stop_out_at=NOW - timedelta(hours=6)),
        _broker(),
        None,
        _settings(),
    )
    assert ok.outcome == "fill"


def test_correlated_positions_gate() -> None:
    v = check_buy(
        _ctx(correlated_pairs=["A-EUR", "B-EUR"]),
        _broker(),
        None,
        _settings(),
    )
    assert v.outcome == "skip" and v.skip_reason == "Correlated positions"
    corr = next(g for g in v.gates if g.name == "Correlated positions")
    assert "A-EUR" in corr.value and "B-EUR" in corr.value
    ok = check_buy(_ctx(correlated_pairs=["A-EUR"]), _broker(), None, _settings())
    assert ok.outcome == "fill"


def test_shock_brake_blocks_entries() -> None:
    v = check_buy(
        _ctx(shock_brake_until=NOW + HOUR),
        _broker(),
        None,
        _settings(),
    )
    assert v.outcome == "skip" and v.skip_reason == "Shock brake"
    ok = check_buy(
        _ctx(shock_brake_until=NOW - timedelta(seconds=1)),
        _broker(),
        None,
        _settings(),
    )
    assert ok.outcome == "fill"


def test_daily_loss_limit() -> None:
    v = check_buy(_ctx(daily_loss_hit=True), _broker(), None, _settings())
    assert v.outcome == "skip" and v.skip_reason == "Daily loss limit"


# ------------------------------------------------------------------ sizing --


def test_sizing_risk_pct_of_equity() -> None:
    v = check_buy(_ctx(risk_pct=Decimal("1")), _broker(), None, _settings())
    assert v.plan is not None
    # 1% of 10000 = 100 EUR risk over the slipped distance 100.05-96 = 4.05
    # → qty ~24.69. Sizing on the fill keeps real risk inside the cap.
    assert v.plan.qty == (Decimal("100") / Decimal("4.05")).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )
    assert v.plan.notional == v.plan.qty * Decimal("100.05")


def test_open_risk_cap_sizes_down_third() -> None:
    """Spec engine test: two full-size positions (1% risk each, stops 4%
    away) leave room for a third at 0.5%."""

    def held(pair: str) -> HeldPosition:
        return HeldPosition(
            pair=pair,
            qty=Decimal("25"),
            entry=Decimal("100"),
            stop=Decimal("96"),
            target=Decimal("110"),
            opened_at=NOW,
            decision_id="d",
            last=Decimal("100"),
            r_unit=Decimal("4"),
        )

    broker = _broker(
        cash="5000",
        positions={
            "A-EUR": held("A-EUR"),
            "B-EUR": held("B-EUR"),
        },
    )
    v = check_buy(_ctx(risk_pct=Decimal("1")), broker, None, _settings())
    assert v.plan is not None
    # Open risk 200 of 250 cap → room 50 over the slipped distance 4.05.
    assert v.plan.qty == (Decimal("50") / Decimal("4.05")).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )
    assert v.plan.notional == v.plan.qty * Decimal("100.05")


def test_open_risk_cap_reached_skips() -> None:
    def held(pair: str) -> HeldPosition:
        return HeldPosition(
            pair=pair,
            qty=Decimal("25"),
            entry=Decimal("100"),
            stop=Decimal("94"),
            target=Decimal("110"),
            opened_at=NOW,
            decision_id="d",
            last=Decimal("100"),
        )

    # 6 EUR risk x 25 qty = 150 each → 300 over the 250 cap.
    broker = _broker(
        cash="4000",
        positions={"A-EUR": held("A-EUR"), "B-EUR": held("B-EUR")},
    )
    v = check_buy(_ctx(), broker, None, _settings())
    assert v.outcome == "skip" and v.skip_reason == "Total open risk"


def test_exposure_and_cash_caps() -> None:
    # Equity 10000 but only 1000 cash and 9000 already exposed →
    # notional cannot exceed ~1000.
    broker = _broker(
        cash="1000",
        positions={
            "Y-EUR": HeldPosition(
                pair="Y-EUR",
                qty=Decimal("90"),
                entry=Decimal("100"),
                stop=Decimal("99.9"),
                target=Decimal("110"),
                opened_at=NOW,
                decision_id="d",
                last=Decimal("100"),
            ),
        },
    )
    v = check_buy(_ctx(), broker, None, _settings())
    assert v.plan is not None
    assert v.plan.notional <= Decimal("1000")


def test_min_trade_eur_skip() -> None:
    v = check_buy(_ctx(), _broker(cash="5"), None, _settings())
    assert v.outcome == "skip" and v.skip_reason == "Sizing"


def test_quantity_precision_rounds_down() -> None:
    market = MarketInfo(
        market="X-EUR",
        status="trading",
        base="X",
        quote="EUR",
        quantity_decimals=0,
    )
    v = check_buy(_ctx(risk_pct=Decimal("1")), _broker(), market, _settings())
    # qty 25 → stays 25 with 0 decimals.
    assert v.plan is not None and v.plan.qty == v.plan.qty.quantize(Decimal("1"))


# ------------------------------------------------------------------- exits --


def _pos(**kw: object) -> HeldPosition:
    base = {
        "pair": "X-EUR",
        "qty": Decimal("1"),
        "entry": Decimal("100"),
        "stop": Decimal("96"),
        "target": Decimal("110"),
        "opened_at": NOW,
        "decision_id": "d",
        "last": Decimal("100"),
        "r_unit": Decimal("4"),
    }
    base.update(kw)
    return HeldPosition(**base)  # type: ignore[arg-type]


def test_stop_and_target_hits() -> None:
    s = _settings()
    assert check_exit(_pos(), Decimal("96"), Decimal("1"), NOW, s) == "stop"
    assert check_exit(_pos(), Decimal("95.99"), Decimal("1"), NOW, s) == "stop"
    assert check_exit(_pos(), Decimal("110"), Decimal("1"), NOW, s) == "target"
    assert check_exit(_pos(), Decimal("105"), Decimal("1"), NOW, s) is None


def test_time_stop() -> None:
    s = _settings()
    old = _pos(opened_at=NOW - timedelta(days=5, seconds=1))
    assert check_exit(old, Decimal("105"), Decimal("1"), NOW, s) == "time_stop"
    young = _pos(opened_at=NOW - timedelta(days=4, hours=23))
    assert check_exit(young, Decimal("105"), Decimal("1"), NOW, s) is None


def test_breakeven_moves_stop_to_entry_plus_fees() -> None:
    s = _settings()
    pos = _pos()
    # price >= entry + 1R (104) → stop moves to entry + round-trip fees.
    check_exit(pos, Decimal("104"), Decimal("1"), NOW, s)
    expected = Decimal("100") * Decimal("1.005")
    assert pos.stop == expected
    # It never moves back down.
    check_exit(pos, Decimal("105"), Decimal("1"), NOW, s)
    assert pos.stop == expected


def test_breakeven_position_counts_zero_open_risk() -> None:
    """After the +1R breakeven move the stop sits above entry, so the
    position adds nothing to the open-risk cap."""
    s = _settings()
    pos = _pos()
    assert pos.open_risk == Decimal("4")  # (100-96) x 1 before the move
    check_exit(pos, Decimal("104"), Decimal("1"), NOW, s)
    assert pos.stop > pos.entry
    assert pos.open_risk == Decimal("0")


def test_trailing_stop_never_moves_down() -> None:
    s = _settings()
    s.risk.trailing_stop_enabled = True
    pos = _pos(highest_close=Decimal("112"))
    # price >= entry + 2R (108); trail = 112 - 2xATR(1) = 110 > stop.
    check_exit(pos, Decimal("109"), Decimal("1"), NOW, s)
    assert pos.stop == Decimal("110")
    pos2 = _pos(stop=Decimal("109"), highest_close=Decimal("110"))
    # trail 108 < current stop 109 → stop stays.
    check_exit(pos2, Decimal("109"), Decimal("1"), NOW, s)
    assert pos2.stop == Decimal("109")


def test_update_trail_tracks_highest_close() -> None:
    pos = _pos()
    update_trail(pos, Decimal("103"))
    assert pos.highest_close == Decimal("103")
    update_trail(pos, Decimal("102"))
    assert pos.highest_close == Decimal("103")

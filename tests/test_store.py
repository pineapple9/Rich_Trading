from datetime import datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from rich_trading.store import Store
from rich_trading.risk import FeeSchedule, RiskLimits


TZ = ZoneInfo("Asia/Bangkok")
T = datetime(2025, 1, 6, 10, tzinfo=TZ)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "local" / "portfolio.db")


def buy(store, ledger="paper", **kwargs):
    args = dict(symbol="DEMO1", quantity=100, entry_price=D("10"),
                entry_fee=D("2"), stop=D("9.9"), target=D("10.5"), opened_at=T)
    args.update(kwargs)
    return store.buy(ledger=ledger, **args)


def test_cash_fees_profit_and_persistence(store):
    pos = buy(store)
    store.mark("paper", "DEMO1", D("10.2"), T + timedelta(minutes=5))
    opened = store.snapshot("paper", as_of=T + timedelta(minutes=5))
    assert opened.cash == D("998")
    assert opened.equity == D("2018")
    assert opened.unrealized_pnl == D("18")
    assert opened.open_risk == D("12")
    assert opened.marks_valid
    store.close(pos, D("10.3"), D("3"), T + timedelta(minutes=10))
    closed = Store(store.path).snapshot("paper", as_of=T + timedelta(minutes=10))
    assert closed.cash == closed.equity == D("2025")
    assert closed.realized_pnl == closed.trades[0]["pnl"] == D("25")
    assert not closed.positions
    assert closed.unrealized_pnl == 0


def test_ledgers_isolated_and_duplicate_reference_scoped(store):
    buy(store, reference="my-order")
    assert store.snapshot("real", as_of=T).cash == D("2000")
    buy(store, ledger="real", reference="my-order")
    with pytest.raises(ValueError, match="reference"):
        buy(store, quantity=1, reference="my-order")


def test_cannot_borrow_and_exit_is_once(store):
    with pytest.raises(ValueError, match="Insufficient cash"):
        buy(store, quantity=200)
    assert store.snapshot("paper", as_of=T).cash == D("2000")
    pos = buy(store)
    store.close(pos, D("9.8"), D("2"), T + timedelta(minutes=5))
    with pytest.raises(ValueError, match="already closed"):
        store.close(pos, D("9.8"), D("2"), T + timedelta(minutes=10))


def test_historical_snapshots_hide_future_marks_and_exits(store):
    pos = buy(store)
    store.mark("paper", "DEMO1", D("10.1"), T + timedelta(minutes=5))
    store.mark("paper", "DEMO1", D("11"), T + timedelta(minutes=15))
    store.close(pos, D("11"), D("2"), T + timedelta(minutes=20))
    prior = store.snapshot("paper", as_of=T - timedelta(seconds=1))
    assert prior.cash == prior.equity == D("2000")
    assert not prior.positions and not prior.trades
    mid = store.snapshot("paper", as_of=T + timedelta(minutes=10))
    assert mid.equity == D("2008")
    assert len(mid.positions) == 1 and not mid.trades
    assert mid.positions[0]["closed_at"] is None
    assert mid.positions[0]["exit_price"] is None
    assert mid.positions[0]["mark_price"] == D("10.1")


def test_missing_and_stale_mark_blocks_new_plans(store):
    buy(store)
    missing = store.snapshot("paper", as_of=T)
    assert not missing.marks_valid
    assert missing.equity == D("1998")
    store.mark("paper", "DEMO1", D("10.2"), T + timedelta(minutes=5))
    stale = store.snapshot("paper", as_of=T + timedelta(days=1))
    assert not stale.marks_valid
    assert stale.positions[0]["mark_price"] == D("10.2")


def test_day_start_includes_carried_inventory_and_overnight_loss(store):
    buy(store)
    store.mark("paper", "DEMO1", D("10.5"), T.replace(hour=16))
    next_day = T + timedelta(days=1)
    store.mark("paper", "DEMO1", D("10"), next_day)
    snapshot = store.snapshot("paper", as_of=next_day)
    assert snapshot.day_start_equity == D("2048")
    assert snapshot.equity == D("1998")
    assert snapshot.day_start_equity - snapshot.equity == D("50")
    assert snapshot.marks_valid


def test_missing_carried_baseline_invalid_even_with_current_quote(store):
    buy(store)
    tomorrow = T + timedelta(days=1)
    store.mark("paper", "DEMO1", D("10.2"), tomorrow)
    snapshot = store.snapshot("paper", as_of=tomorrow)
    assert snapshot.day_start_equity == D("1998")
    assert not snapshot.marks_valid


def test_intraday_realized_loss_in_day_baseline(store):
    pos = buy(store)
    store.close(pos, D("9.7"), D("2"), T + timedelta(minutes=5))
    snap = store.snapshot("paper", as_of=T + timedelta(minutes=6))
    assert snap.day_start_equity == D("2000")
    assert snap.equity == D("1966")


def test_timezone_and_chronological_guards(store):
    with pytest.raises(ValueError, match="timezone-aware"):
        buy(store, opened_at=T.replace(tzinfo=None))
    pos = buy(store)
    with pytest.raises(ValueError, match="precede"):
        store.close(pos, D("10"), D("0"), T - timedelta(minutes=1))
    store.close(pos, D("10"), D("0"), T + timedelta(minutes=10))
    with pytest.raises(ValueError, match="chronologically"):
        buy(store, opened_at=T + timedelta(minutes=5))


@pytest.mark.parametrize("field,value", [
    ("entry_price", "NaN"), ("entry_fee", "Infinity"),
    ("entry_fee", "-1"), ("quantity", 1.5), ("quantity", True),
    ("quantity", 0), ("stop", "10"), ("symbol", "oops';--"),
])
def test_invalid_fills_not_written(store, field, value):
    with pytest.raises(ValueError):
        buy(store, **{field: value})
    assert store.snapshot("paper", as_of=T).cash == D("2000")


def test_decimal_precision_and_settings(store):
    buy(store, quantity=3, entry_price=D("0.1"), entry_fee=D("0.01"),
        stop=D("0.09"), target=D("0.12"))
    snap = store.snapshot("paper", as_of=T)
    assert snap.cash == D("1999.69")
    store.save_settings({"fee": "0.0015", "confirmed": False, "label": "ค่าธรรมเนียม"})
    assert Store(store.path).load_settings()["label"] == "ค่าธรรมเนียม"
    with pytest.raises(ValueError):
        store.save_settings({"bad": float("nan")})


def test_exact_midnight_fill_not_subtracted_from_opening_equity(store):
    midnight = T.replace(hour=0)
    buy(store, opened_at=midnight)
    store.mark("paper", "DEMO1", D("10"), midnight)
    snapshot = store.snapshot("paper", as_of=T)
    assert snapshot.day_start_equity == D("2000")
    assert snapshot.equity == D("1998")


FREE = FeeSchedule(D("0"), D("0"), D("0"), D("0"))


def paper_plan(store, **overrides):
    args = dict(symbol="DEMO1", entry=D("1"), stop=D("0.9"),
                target=D("1.4"), lot_size=100, fees=FREE,
                limits=RiskLimits(), as_of=T)
    args.update(overrides)
    return store.buy_paper_plan(**args)


def test_atomic_paper_fill_uses_slipped_cost_but_reference_quote(store):
    pos_id = paper_plan(store)
    snap = store.snapshot("paper", as_of=T)
    position = snap.positions[0]
    assert position["id"] == pos_id
    assert position["quantity"] == 100
    assert position["entry_price"] == D("1.001")
    assert position["mark_price"] == D("1")
    assert snap.cash == D("1899.90")
    assert snap.equity == D("1999.90")
    assert snap.marks_valid
    assert store.snapshot("real", as_of=T).equity == D("2000")


def test_atomic_plan_cannot_reuse_risk_from_prior_approval(store):
    paper_plan(store)
    with pytest.raises(ValueError):
        paper_plan(store)
    assert len(store.snapshot("paper", as_of=T).positions) == 1


def test_atomic_plan_serializes_concurrent_attempts(store):
    from concurrent.futures import ThreadPoolExecutor

    def attempt():
        try:
            return paper_plan(store)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(store.snapshot("paper", as_of=T).positions) == 1


def test_atomic_plan_reserves_exit_fees_not_only_gross_stop_distance(store):
    fees = FeeSchedule(D("0"), D("0"), D("3"), D("0"))
    buy(store, quantity=100, entry_price=D("1"), entry_fee=D("3"),
        stop=D("0.94"), target=D("2"))
    store.mark("paper", "DEMO1", D("1"), T)
    paper_plan(store, symbol="DEMO2", stop=D("0.99"), target=D("2"),
               fees=fees, slippage_rate=D("0"))
    snap = store.snapshot("paper", as_of=T)
    # Existing risk = 6 price loss + 3 entry + 3 exit = 12. Remaining
    # budget 8 permits 200 shares (risk 8), not 500 (risk 11).
    assert snap.positions[1]["quantity"] == 200


@pytest.mark.parametrize("age,allowed", [(15, True), (16, False)])
def test_atomic_plan_requires_quotes_no_older_than_fifteen_minutes(store, age, allowed):
    buy(store, quantity=1, entry_price=D("1"), entry_fee=D("0"),
        stop=D("0.999"), target=D("1.1"))
    store.mark("paper", "DEMO1", D("1"), T)
    if allowed:
        paper_plan(store, symbol="DEMO2", as_of=T + timedelta(minutes=age))
        assert len(store.snapshot("paper", as_of=T + timedelta(minutes=age)).positions) == 2
    else:
        with pytest.raises(ValueError, match="ราคา"):
            paper_plan(store, symbol="DEMO2", as_of=T + timedelta(minutes=age))


def test_atomic_plan_cannot_bypass_fee_type_or_unaffordable_cost(store):
    with pytest.raises(ValueError):
        paper_plan(store, fees=None)
    with pytest.raises(ValueError):
        paper_plan(store, fees=FeeSchedule())
    with pytest.raises(ValueError):
        paper_plan(store, entry=D("30"), stop=D("29.9"), target=D("31"))
    assert not store.snapshot("paper", as_of=T).positions


def test_atomic_plan_total_cost_is_rounded_to_satang(store):
    paper_plan(store, entry=D("0.3333"), stop=D("0.33"), target=D("0.35"),
               lot_size=1, limits=RiskLimits(risk_per_trade=D("1")))
    snap = store.snapshot("paper", as_of=T)
    assert snap.cash == snap.cash.quantize(D("0.01"))
    position = snap.positions[0]
    cost = position["quantity"] * position["entry_price"] + position["entry_fee"]
    assert cost == D("2000") - snap.cash


def test_atomic_plan_blocks_after_daily_limit_and_rejects_backdated_fills(store):
    pos_id = buy(store, entry_fee=D("0"))
    store.close(pos_id, D("9.6"), D("0"), T + timedelta(minutes=1))
    with pytest.raises(ValueError, match="รายวัน"):
        paper_plan(store, as_of=T + timedelta(minutes=2))
    with pytest.raises(ValueError, match="chronologically"):
        paper_plan(store, as_of=T)

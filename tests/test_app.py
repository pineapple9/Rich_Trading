"""Functional UI checks using Streamlit's real script runner and SQLite."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from streamlit.testing.v1 import AppTest

from rich_trading.market import load_bars
from rich_trading.store import Store


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ui(tmp_path, monkeypatch):
    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("RICH_TRADING_DB", str(path))
    return AppTest.from_file(str(ROOT / "app.py")), Store(path)


def navigate(app, page):
    app.radio[0].set_value(page).run()
    assert not app.exception, [error.message for error in app.exception]


def test_all_pages_render_and_default_plan_is_rejected(ui):
    app, _ = ui
    app.run(timeout=30)
    assert not app.exception
    assert app.metric[0].value == "฿2,000.00"
    for page in ["แผนเทรด", "พอร์ตจำลอง", "บันทึกเงินจริง", "ทดสอบย้อนหลัง", "ตั้งค่า"]:
        navigate(app, page)
    navigate(app, "แผนเทรด")
    assert next(button for button in app.button if button.label == "บันทึกซื้อในพอร์ตจำลอง").disabled
    assert any("ยังไม่มีรายการที่เหมาะสม" in warning.value for warning in app.warning)


def test_fee_settings_survive_restart(ui):
    app, store = ui
    app.run()
    navigate(app, "ตั้งค่า")
    next(field for field in app.number_input if field.label == "ค่าคอมขั้นต่ำต่อคำสั่ง (บาท)").set_value(0.0)
    app.checkbox[0].check()
    app.button[0].click().run()
    assert not app.exception
    assert store.load_settings()["minimum_per_order"] == "0.0"
    assert store.load_settings()["fees_confirmed"] is True
    fresh = AppTest.from_file(str(ROOT / "app.py")).run()
    assert not fresh.exception
    assert not any("กำลังใช้ค่าธรรมเนียมสมมติ" in w.value for w in fresh.warning)


def test_plan_buy_close_and_ledger_isolation(ui):
    app, store = ui
    store.save_settings({"minimum_per_order": "0", "commission_rate": "0", "other_rate": "0", "vat_rate": "0"})
    app.run()
    navigate(app, "แผนเทรด")
    buy = next(button for button in app.button if button.label == "บันทึกซื้อในพอร์ตจำลอง")
    assert not buy.disabled
    buy.click().run()
    assert not app.exception
    as_of = load_bars(ROOT / "data" / "sample_intraday.csv").timestamp.max().to_pydatetime() + timedelta(minutes=5)
    opened = store.snapshot("paper", as_of=as_of)
    assert len(opened.positions) == 1
    assert opened.cash < Decimal("2000")
    assert store.snapshot("real").cash == Decimal("2000")
    navigate(app, "พอร์ตจำลอง")
    assert opened.equity < Decimal("2000")  # entry slippage is an immediate loss
    assert app.metric[0].delta.startswith("-")  # Streamlit must display a loss as down/red
    next(button for button in app.button if button.label == "บันทึกขายจำลอง").click().run()
    assert not app.exception
    closed = store.snapshot("paper", as_of=as_of)
    assert len(closed.trades) == 1 and not closed.positions
    assert closed.realized_pnl < 0  # adverse entry AND exit slippage
    assert closed.equity == Decimal("2000") + closed.realized_pnl


def test_paper_exit_fee_recomputed_after_price_change(ui):
    app, store = ui
    store.save_settings({"minimum_per_order": "0"})
    as_of = load_bars(ROOT / "data" / "sample_intraday.csv").timestamp.max().to_pydatetime() + timedelta(minutes=5)
    pid = store.buy("paper", "DEMO1", 100, 1, 0, Decimal("0.9"), 2, as_of)
    store.mark("paper", "DEMO1", 1, as_of)
    app.run()
    navigate(app, "พอร์ตจำลอง")
    app.number_input(key=f"exit_price_paper_{pid}").set_value(10.0).run()
    next(button for button in app.button if button.label == "บันทึกขายจำลอง").click().run()
    assert not app.exception
    trade = store.snapshot("paper", as_of=as_of).trades[0]
    assert trade["exit_fee"] == Decimal("1.61")


def test_backtest_runs_real_signals_not_zero_test_placeholder(ui):
    app, _ = ui
    app.run()
    navigate(app, "ทดสอบย้อนหลัง")
    app.button[0].click().run()
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["สัญญาณทั้งหมด"] == "4"
    assert metrics["รายการที่เทรดได้"] == "0"  # default illustrative fees exceed budget
    assert metrics["เงินปลายการทดสอบ"] == "฿2,000.00"


def test_backtest_reports_completed_trades_in_hypothetical_zero_fee_scenario(ui):
    app, store = ui
    store.save_settings({"commission_rate": "0", "minimum_per_order": "0", "vat_rate": "0"})
    app.run()
    navigate(app, "ทดสอบย้อนหลัง")
    app.button[0].click().run()
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["รายการที่เทรดได้"] == "2"
    assert metrics["กำไรสุทธิ"] == "฿62.26"
    assert metrics["Drawdown ณ จุดปิดรายการ"] == "0.00%"


def test_actual_journal_records_historical_exit_date_without_price_file(ui):
    app, store = ui
    tz = ZoneInfo("Asia/Bangkok")
    pid = store.buy("real", "TEST", 100, 1, 1, Decimal("0.9"), 2,
                    datetime(2025, 1, 6, 10, tzinfo=tz))
    app.run()
    navigate(app, "บันทึกเงินจริง")
    app.radio[1].set_value("อัปโหลด CSV").run()  # real journal must remain independent
    next(field for field in app.date_input if field.label == "วันที่ขายจริง (ไทย)").set_value(
        datetime(2025, 1, 7).date())
    next(field for field in app.time_input if field.label == "เวลาขายจริง (ไทย)").set_value(
        datetime(2025, 1, 7, 11).time())
    next(button for button in app.button if button.label == "บันทึกขายจริง").click().run()
    assert not app.exception
    trade = store.snapshot("real").trades[0]
    assert trade["id"] == pid
    assert trade["closed_at"].astimezone(tz) == datetime(2025, 1, 7, 11, tzinfo=tz)


def test_stale_candidate_is_blocked(ui, monkeypatch):
    import rich_trading.market as market

    app, store = ui
    store.save_settings({"minimum_per_order": "0", "commission_rate": "0", "vat_rate": "0"})
    bars = load_bars(ROOT / "data" / "sample_intraday.csv")
    # DEMO1 is a full day behind DEMO2. Global paper clock must not re-date its price.
    day = datetime(2025, 1, 7, tzinfo=ZoneInfo("Asia/Bangkok")).date()
    bars = bars[~((bars.symbol == "DEMO1") & (bars.timestamp.dt.date == day))]
    monkeypatch.setattr(market, "load_bars", lambda _: bars)
    app.run()
    navigate(app, "แผนเทรด")
    assert app.selectbox(key="plan_symbol").value == "DEMO1"
    assert next(button for button in app.button if button.label == "บันทึกซื้อในพอร์ตจำลอง").disabled
    assert any("เก่ากว่าเวลาจำลอง" in warning.value for warning in app.warning)

"""Hosted-demo boundaries exercised through the real Streamlit script runner.

These checks cover behavior and session isolation, not a device viewport.
"""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

from rich_trading.store import Store


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "cloud" / "mobile_demo.py"


def run_demo():
    app = AppTest.from_file(str(ENTRYPOINT)).run(timeout=30)
    assert not app.exception, [error.message for error in app.exception]
    return app


def navigate(app, page):
    menu = next(radio for radio in app.radio if radio.label == "พื้นที่ทำงาน")
    menu.set_value(page).run()
    assert not app.exception, [error.message for error in app.exception]


def field(app, label):
    return next(item for item in app.number_input if item.label == label)


def button(app, label):
    return next(item for item in app.button if item.label == label)


def set_hypothetical_zero_fees(app):
    navigate(app, "ตั้งค่า")
    for label in ["ค่าคอมมิชชัน (%)", "ค่าธรรมเนียมอื่นรวม (%)",
                  "ค่าคอมขั้นต่ำต่อคำสั่ง (บาท)", "VAT บนค่าธรรมเนียม (%)"]:
        field(app, label).set_value(0.0)
    button(app, "บันทึกการตั้งค่า").click().run()
    assert not app.exception


def metric(app, label):
    return next(item.value for item in app.metric if item.label == label)


def test_mobile_entrypoint_renders_without_actual_journal_or_real_order_actions():
    app = run_demo()
    menu = next(radio for radio in app.radio if radio.label == "พื้นที่ทำงาน")
    assert "บันทึกเงินจริง" not in menu.options
    assert metric(app, "มูลค่าพอร์ต") == "฿2,000.00"
    for page in ["แผนเทรด", "พอร์ตจำลอง", "ทดสอบย้อนหลัง", "ตั้งค่า"]:
        navigate(app, page)
        assert not any(item.label in {"บันทึกประวัติซื้อจริง", "บันทึกขายจริง"}
                       for item in app.button)


def test_demo_never_opens_or_changes_configured_private_database(tmp_path, monkeypatch):
    path = tmp_path / "private.sqlite3"
    private = Store(path)
    private.save_settings({"minimum_per_order": "123", "risk_per_trade": "77"})
    private.buy("real", "PRIVATE", 100, Decimal("1"), Decimal("0"),
                Decimal("0.9"), Decimal("2"),
                datetime(2025, 1, 7, 10, tzinfo=ZoneInfo("Asia/Bangkok")),
                notes="SYNTHETIC_PRIVATE_JOURNAL_SENTINEL")
    original_bytes = path.read_bytes()
    monkeypatch.setenv("RICH_TRADING_DB", str(path))
    original_init = Store.__init__
    opened_paths = []

    def track_database(self, database_path):
        opened_paths.append(Path(database_path).resolve())
        return original_init(self, database_path)

    monkeypatch.setattr(Store, "__init__", track_database)
    app = run_demo()
    navigate(app, "ตั้งค่า")
    assert field(app, "ค่าคอมขั้นต่ำต่อคำสั่ง (บาท)").value == 20.0
    assert field(app, "งบความเสี่ยงต่อครั้ง (บาท)").value == 20.0
    set_hypothetical_zero_fees(app)
    navigate(app, "แผนเทรด")
    buy = button(app, "บันทึกซื้อในพอร์ตจำลอง")
    assert not buy.disabled
    buy.click().run()
    assert not app.exception
    navigate(app, "พอร์ตจำลอง")
    assert metric(app, "เงินสดพร้อมใช้") != "฿2,000.00"
    assert path.resolve() not in opened_paths
    assert opened_paths  # The demo actually used its own database.
    assert path.read_bytes() == original_bytes


def test_demo_sessions_isolate_settings_and_positions_but_reruns_preserve_them():
    first = run_demo()
    set_hypothetical_zero_fees(first)
    navigate(first, "แผนเทรด")
    button(first, "บันทึกซื้อในพอร์ตจำลอง").click().run()
    assert not first.exception
    navigate(first, "พอร์ตจำลอง")
    first_cash = metric(first, "เงินสดพร้อมใช้")
    assert first_cash != "฿2,000.00"
    assert any(item.label == "บันทึกขายจำลอง" for item in first.button)

    second = run_demo()
    navigate(second, "ตั้งค่า")
    assert field(second, "ค่าคอมขั้นต่ำต่อคำสั่ง (บาท)").value == 20.0
    navigate(second, "พอร์ตจำลอง")
    assert metric(second, "เงินสดพร้อมใช้") == "฿2,000.00"
    assert not any(item.label == "บันทึกขายจำลอง" for item in second.button)

    # The first visitor's state remains available after other sessions run.
    navigate(first, "ภาพรวม")
    navigate(first, "พอร์ตจำลอง")
    assert metric(first, "เงินสดพร้อมใช้") == first_cash
    assert any(item.label == "บันทึกขายจำลอง" for item in first.button)
    navigate(first, "ตั้งค่า")
    assert field(first, "ค่าคอมขั้นต่ำต่อคำสั่ง (บาท)").value == 0.0

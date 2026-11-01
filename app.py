"""Local Streamlit UI. No brokerage connection or automatic order submission."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import os
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from rich_trading.market import load_bars
from rich_trading.risk import D, FeeSchedule, PortfolioState, RiskLimits, plan_trade
from rich_trading.store import Store
from rich_trading.strategy import backtest_signals, opening_range_signals


ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Bangkok")
SLIPPAGE = Decimal("0.001")
DEFAULTS = {
    "commission_rate": "0.0015", "other_rate": "0",
    "minimum_per_order": "20", "vat_rate": "0.07",
    "risk_per_trade": "20", "daily_loss_limit": "40",
    "fees_confirmed": False,
}


def baht(value):
    # Streamlit determines delta direction from the leading sign. Keeping the
    # minus before the currency symbol makes portfolio losses red/downward.
    return f"{'-' if value < 0 else ''}฿{abs(value):,.2f}"


def records_frame(records):
    """Use display strings for exact Decimal values (Arrow needs uniform types)."""
    return pd.DataFrame([
        {key: str(value) if isinstance(value, Decimal) else value for key, value in row.items()}
        for row in records
    ])


def fees_from(settings):
    return FeeSchedule(**{key: D(settings[key]) for key in (
        "commission_rate", "other_rate", "minimum_per_order", "vat_rate"
    )})


def state_from(snapshot, fees, as_of):
    # Reserve the complete original entry-to-stop risk, including exit costs.
    # Using entry rather than current profit is deliberately conservative.
    reserve = Decimal("0")
    for position in snapshot.positions:
        qty = position["quantity"]
        stop_value = position["stop"] * (1 - SLIPPAGE) * qty
        reserve += max(Decimal("0"), position["entry_price"] * qty
                       + position["entry_fee"] - stop_value + fees.fee(stop_value))
    fresh = snapshot.marks_valid and all(
        p["marked_at"] is not None
        and timedelta(0) <= as_of - p["marked_at"] <= timedelta(minutes=15)
        for p in snapshot.positions
    )
    return PortfolioState(snapshot.cash, snapshot.equity, snapshot.day_start_equity,
                          reserve, fresh)


def render_metrics(snapshot, limits):
    columns = st.columns(4)
    columns[0].metric("มูลค่าพอร์ต", baht(snapshot.equity),
                      baht(snapshot.equity - limits.initial_capital))
    columns[1].metric("เงินสดพร้อมใช้", baht(snapshot.cash))
    columns[2].metric("กำไร/ขาดทุนปิดแล้ว", baht(snapshot.realized_pnl))
    columns[3].metric("กำไร/ขาดทุนที่ยังไม่ปิด", baht(snapshot.unrealized_pnl))
    daily_loss = max(Decimal("0"), snapshot.day_start_equity - snapshot.equity)
    total_loss = max(Decimal("0"), limits.initial_capital - snapshot.equity)
    total_limit = limits.initial_capital * limits.total_loss_fraction
    if daily_loss >= limits.daily_loss_limit or total_loss >= total_limit:
        st.error("ถึงเกณฑ์หยุดเปิดสถานะใหม่ — ทบทวนพอร์ตก่อนดำเนินการต่อ")
    if not snapshot.marks_valid:
        st.warning("ราคาประเมินสถานะที่เปิดอยู่ไม่ครบหรือเก่า งดวางแผนเปิดสถานะใหม่จนกว่าจะอัปเดตราคา")
    st.caption(f"ขาดทุนวันนี้ {baht(daily_loss)} / {baht(limits.daily_loss_limit)} · "
               f"ขาดทุนจากทุนเริ่มต้น {baht(total_loss)} / {baht(total_limit)} · "
               "รวมผลของสถานะที่ยังไม่ปิดและค่าธรรมเนียมที่บันทึกแล้ว")


def show_settings(store, settings, *, hosted_demo=False):
    st.subheader("ต้นทุนและขอบเขตความเสี่ยง")
    st.info("ตัวเลขเริ่มต้นเป็นสถานการณ์สมมติ ไม่ใช่อัตราค่าธรรมเนียม BLS "
            "โมเดลนี้คิดขั้นต่ำต่อคำสั่งและ VAT จากค่าธรรมเนียมรวม "
            "หากบัญชีคิดขั้นต่ำรายวันหรือมีเงื่อนไขอื่น ต้องปรับโมเดลก่อนใช้ประเมินเงินจริง")
    with st.form("settings"):
        a, b = st.columns(2)
        commission = a.number_input("ค่าคอมมิชชัน (%)", min_value=0.0, max_value=100.0,
                                    value=float(D(settings["commission_rate"]) * 100), format="%.4f")
        other = b.number_input("ค่าธรรมเนียมอื่นรวม (%)", min_value=0.0, max_value=100.0,
                               value=float(D(settings["other_rate"]) * 100), format="%.4f")
        minimum = a.number_input("ค่าคอมขั้นต่ำต่อคำสั่ง (บาท)", min_value=0.0,
                                 value=float(settings["minimum_per_order"]), step=1.0)
        vat = b.number_input("VAT บนค่าธรรมเนียม (%)", min_value=0.0, max_value=100.0,
                             value=float(D(settings["vat_rate"]) * 100))
        risk = a.number_input("งบความเสี่ยงต่อครั้ง (บาท)", min_value=0.01,
                              max_value=600.0, value=float(settings["risk_per_trade"]))
        daily = b.number_input("จุดหยุดขาดทุนรายวัน (บาท)", min_value=0.01,
                               max_value=600.0, value=float(settings["daily_loss_limit"]))
        st.caption("ทุนเริ่มต้น 2,000 บาท · หยุดทบทวนเมื่อขาดทุน 30% (600 บาท) · "
                   "ประมาณ slippage ฝั่งละ 0.10% · อัตราผลตอบแทนสุทธิ/ความเสี่ยงขั้นต่ำ 1.5")
        confirmation_label = ("ใช้ค่าธรรมเนียมชุดนี้ในการทดลอง" if hosted_demo else
                              "ฉันตรวจสอบแล้วว่าอัตราและวิธีคิดนี้ตรงกับบัญชีของฉัน")
        confirmed = st.checkbox(confirmation_label,
                                 value=settings["fees_confirmed"])
        if st.form_submit_button("บันทึกการตั้งค่า", type="primary"):
            updated = {
                "commission_rate": str(D(commission) / 100), "other_rate": str(D(other) / 100),
                "minimum_per_order": str(minimum), "vat_rate": str(D(vat) / 100),
                "risk_per_trade": str(risk), "daily_loss_limit": str(daily),
                "fees_confirmed": confirmed,
            }
            store.save_settings(updated)
            st.rerun()


def show_overview(bars, snapshot, limits, source_name):
    render_metrics(snapshot, limits)
    st.divider()
    left, right = st.columns([2, 1])
    with left:
        st.subheader("ดูข้อมูลก่อนวางแผน")
        symbol = st.selectbox("หุ้นในชุดข้อมูล", sorted(bars.symbol.unique()), key="chart_symbol")
        selected = bars[bars.symbol == symbol].set_index("timestamp")
        st.line_chart(selected[["close"]], color="#36D6B0")
        st.caption("กราฟราคาปิดของแท่งข้อมูลนำเข้า ไม่ใช่ราคาสตรีมจาก Streaming")
    with right:
        st.subheader("แผนสำหรับบัญชีของคุณ")
        st.write("BLS · Streaming · Cash Balance")
        st.write("เงินทุน 2,000 บาท · ซื้อด้วยเงินสด · ยืนยันคำสั่งเอง")
        st.write("เริ่มหุ้นไทย ส่วนหุ้นสหรัฐฯ อยู่ในแผนระยะถัดไป")
        st.caption(f"แหล่งข้อมูล: {source_name}")
        st.caption("การบันทึกในแอปนี้ไม่ส่งคำสั่งและไม่ดึงยอดบัญชีจาก BLS")
    st.subheader("วิธีเริ่มทดลอง")
    st.write("1. ตรวจต้นทุนในหน้าตั้งค่า → 2. ดูแผนเทรด → "
             "3. ทดลองบันทึกในพอร์ตจำลอง → 4. ตรวจผลหลังต้นทุน")


def show_planner(store, bars, snapshot, fees, limits, as_of):
    st.subheader("แผนเทรดสมมติจากข้อมูลนำเข้า")
    st.caption("ใช้เพื่อวิจัยและทดลอง ไม่ใช่สัญญาณราคาปัจจุบันสำหรับส่งคำสั่งจริง")
    with st.expander("คัดกรองหุ้นและดูสัญญาณย้อนหลัง"):
        latest_day = bars.timestamp.dt.date.max()
        today = bars[bars.timestamp.dt.date == latest_day].copy()
        today["turnover_estimate"] = today.close * today.volume
        screen = today.groupby("symbol").agg(
            last_close=("close", "last"), volume=("volume", "sum"),
            turnover_estimate=("turnover_estimate", "sum"), lot_size=("lot_size", "last"),
        ).reset_index()
        minimum_turnover = st.number_input("มูลค่าซื้อขายโดยประมาณขั้นต่ำในวันที่นำเข้า (บาท)",
                                            min_value=0.0, value=0.0, step=10000.0)
        st.dataframe(screen[screen.turnover_estimate >= minimum_turnover],
                     hide_index=True, width="stretch")
        st.caption("มูลค่าประมาณจาก close × volume ของแต่ละแท่ง "
                   "CSV นี้ไม่มี bid/ask จึงยังตรวจส่วนต่างราคาซื้อ–ขายไม่ได้")
        try:
            signals = opening_range_signals(bars)
            st.dataframe(signals, hide_index=True, width="stretch")
            st.caption("สัญญาณวิจัยย้อนหลังจากทุกวันที่นำเข้า; ราคาเข้าเป็น open แท่งถัดไป "
                       "ไม่ใช่คำแนะนำให้ซื้อ ณ เวลาปัจจุบัน")
        except ValueError as exc:
            st.warning(str(exc))
    symbol = st.selectbox("สัญลักษณ์", sorted(bars.symbol.unique()), key="plan_symbol")
    last = bars[bars.symbol == symbol].iloc[-1]
    price = float(last.close)
    a, b, c = st.columns(3)
    entry = a.number_input("ราคาเข้าอ้างอิง", min_value=0.0001, value=price,
                            format="%.4f", key=f"entry_{symbol}")
    stop = b.number_input("จุดตัดขาดทุน", min_value=0.0001, value=round(price * 0.99, 4),
                          format="%.4f", key=f"stop_{symbol}")
    target = c.number_input("ราคาเป้าหมาย", min_value=0.0001, value=round(price * 1.03, 4),
                            format="%.4f", key=f"target_{symbol}")
    lot = st.number_input("จำนวนหุ้นต่อ lot ของหลักทรัพย์นี้", min_value=1,
                          value=int(last.lot_size), step=1, key=f"lot_{symbol}")
    st.caption("ตรวจ lot และช่วงราคาที่ส่งคำสั่งได้กับโบรกเกอร์ "
               "จำนวน lot ใน CSV ไม่ใช่การยืนยันกฎของตลาด")
    quote_time = last.timestamp.to_pydatetime() + timedelta(minutes=5)
    candidate_fresh = timedelta(0) <= as_of - quote_time <= timedelta(minutes=15)
    if not candidate_fresh:
        st.warning("ข้อมูลล่าสุดของหุ้นนี้เก่ากว่าเวลาจำลองเกิน 15 นาที กรุณานำเข้าข้อมูลที่สอดคล้องกันก่อน")
    try:
        state = state_from(snapshot, fees, as_of)
        state = replace(state, marks_valid=state.marks_valid and candidate_fresh)
        plan = plan_trade(symbol, D(entry), D(stop), D(target), int(lot), fees, limits,
                          state)
    except ValueError as exc:
        st.error(str(exc))
        return
    if plan.approved:
        st.success("ผ่านกติกาจำลอง — สามารถบันทึกเข้าพอร์ตจำลองได้")
    else:
        st.warning("ยังไม่มีรายการที่เหมาะสม")
        for reason in plan.reasons:
            st.write(f"• {reason}")
    columns = st.columns(4)
    columns[0].metric("จำนวนหุ้น", f"{plan.quantity:,}")
    columns[1].metric("เงินซื้อรวมต้นทุน", baht(plan.total_cost))
    columns[2].metric("ขาดทุนประมาณที่ stop", baht(plan.risk))
    columns[3].metric("กำไรสุทธิที่เป้าหมาย", baht(plan.net_reward))
    st.caption(f"ค่าซื้อ {baht(plan.entry_fee)} · ค่าขายประมาณ ณ stop {baht(plan.exit_fee)} · "
               f"ผลตอบแทน/ความเสี่ยง {plan.reward_risk:.2f} เท่า · "
               "รวม slippage ฝั่งละ 0.10%; stop ไม่รับประกันราคาที่ขายได้จริง")
    if st.button("บันทึกซื้อในพอร์ตจำลอง", type="primary", disabled=not plan.approved):
        try:
            # Sizing, cash, risk recheck and insertion share one SQLite transaction.
            store.buy_paper_plan(symbol, D(entry), D(stop), D(target), int(lot),
                                 fees, limits, as_of)
            st.session_state["flash"] = "บันทึกซื้อจำลองแล้ว ไม่มีการส่งคำสั่งไป BLS"
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def show_portfolio(store, ledger, snapshot, bars, fees, limits, as_of):
    real = ledger == "real"
    st.subheader("สมุดบันทึกเงินจริง" if real else "พอร์ตจำลอง")
    if real:
        st.info("บันทึกสิ่งที่เกิดขึ้นจริงจาก Streaming เท่านั้น "
                "ระบบไม่ตรวจสอบกับ BLS และไม่ส่งคำสั่ง ยอดที่นี่เริ่มต้น 2,000 บาท "
                "ไม่รองรับฝาก–ถอนหรือโอนหลักทรัพย์ในเวอร์ชันนี้")
    else:
        st.caption(f"เวลาจำลองตามชุดข้อมูล: {as_of.astimezone(TZ):%d/%m/%Y %H:%M} น. "
                   "ราคาที่กรอกเองเป็นสมมติฐานของผู้ทดลอง")
    render_metrics(snapshot, limits)
    if real:
        with st.expander("บันทึกรายการซื้อที่เกิดขึ้นจริง"):
            st.caption("กรอกค่าธรรมเนียมจริงจากรายการยืนยัน "
                       "การบันทึกประวัติจะไม่ถูกปิดกั้นด้วยกฎแนะนำเทรด")
            with st.form("actual_buy", clear_on_submit=True):
                a, b = st.columns(2)
                symbol = a.text_input("ชื่อหุ้นจริง", placeholder="เช่น ชื่อย่อใน Streaming")
                quantity = b.number_input("จำนวนหุ้นจริง", min_value=1, value=100, step=1)
                price = a.number_input("ราคาซื้อจริง", min_value=0.0001, value=1.0, format="%.4f")
                fee = b.number_input("ค่าซื้อจริงรวม VAT (บาท)", min_value=0.0, value=0.0)
                stop = a.number_input("Stop ที่วางแผนไว้", min_value=0.0001, value=0.95, format="%.4f")
                target = b.number_input("เป้าหมายที่วางแผนไว้", min_value=0.0001, value=1.10, format="%.4f")
                day = a.date_input("วันที่ซื้อ (ไทย)", value=as_of.astimezone(TZ).date())
                clock = b.time_input("เวลาซื้อ (ไทย)", value=as_of.astimezone(TZ).time().replace(tzinfo=None))
                reference = st.text_input("เลขอ้างอิงรายการ (ใช้ตรวจรายการซ้ำ)")
                notes = st.text_input("เหตุผลที่เข้า / บันทึก")
                if st.form_submit_button("บันทึกประวัติซื้อจริง"):
                    event_time = datetime.combine(day, clock, tzinfo=TZ)
                    try:
                        if event_time > as_of:
                            raise ValueError("เวลาซื้อจริงต้องไม่อยู่ในอนาคต")
                        store.buy("real", symbol, quantity, D(price), D(fee), D(stop),
                                  D(target), event_time, notes=notes, reference=reference)
                        st.session_state["flash"] = "บันทึกประวัติแล้ว กรุณาอัปเดตราคาประเมินพอร์ต"
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
    st.markdown("**สถานะที่ยังไม่ปิด**")
    if snapshot.positions:
        st.dataframe(records_frame(snapshot.positions), hide_index=True, width="stretch")
        with st.expander("อัปเดตราคาประเมิน / บันทึกขายทั้งสถานะ"):
            positions = {p["id"]: p for p in snapshot.positions}
            selected = st.selectbox("เลือกรายการ", list(positions),
                                     format_func=lambda pid: f"#{pid} · {positions[pid]['symbol']}",
                                     key=f"position_{ledger}")
            position = positions[selected]
            price = st.number_input("ราคาประเมินหรือราคาขาย", min_value=0.0001,
                                     value=float(position["mark_price"]), format="%.4f",
                                     key=f"exit_price_{ledger}_{selected}")
            a, b = st.columns(2)
            if a.button("อัปเดตราคาประเมิน", key=f"mark_{ledger}"):
                try:
                    store.mark(ledger, position["symbol"], D(price), as_of)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            estimated = fees.fee(D(price) * position["quantity"] * (1 - SLIPPAGE))
            if real:
                exit_fee = D(st.number_input("ค่าขายรวม VAT (บาท)", min_value=0.0,
                                             value=0.0, key=f"exit_fee_{ledger}_{selected}"))
                exit_day = st.date_input("วันที่ขายจริง (ไทย)", value=as_of.astimezone(TZ).date())
                exit_clock = st.time_input("เวลาขายจริง (ไทย)",
                                            value=as_of.astimezone(TZ).time().replace(tzinfo=None))
                exit_time = datetime.combine(exit_day, exit_clock, tzinfo=TZ)
            else:
                exit_fee = estimated
                exit_time = as_of
                st.caption(f"ค่าขายจำลองคำนวณตามราคาที่กรอก: {baht(exit_fee)}")
            st.caption("ขายเต็มจำนวนเท่านั้นในเวอร์ชันแรก; "
                       + ("กรอกเวลาและค่าขายจริงจากรายการยืนยัน" if real else
                          "ราคาขายจำลองจะหัก slippage 0.10% เพิ่มจากราคาที่กรอก"))
            if b.button("บันทึกขายจริง" if real else "บันทึกขายจำลอง", key=f"sell_{ledger}"):
                try:
                    fill = D(price) if real else D(price) * (1 - SLIPPAGE)
                    if exit_time > as_of:
                        raise ValueError("เวลาขายจริงต้องไม่อยู่ในอนาคต")
                    store.close(selected, fill, exit_fee, exit_time)
                    st.session_state["flash"] = "บันทึกปิดสถานะแล้ว"
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        if not real and st.button("อัปเดตราคาจำลองจาก CSV"):
            try:
                for symbol, group in bars.groupby("symbol"):
                    row = group.iloc[-1]
                    store.mark("paper", symbol, D(row.close),
                               row.timestamp.to_pydatetime() + timedelta(minutes=5))
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    else:
        st.info("ยังไม่มีสถานะที่เปิดอยู่")
    st.markdown("**รายการที่ปิดแล้ว**")
    if snapshot.trades:
        frame = records_frame(snapshot.trades)
        st.dataframe(frame, hide_index=True, width="stretch")
        st.download_button("ดาวน์โหลดประวัติ CSV", frame.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"{ledger}_trades.csv", mime="text/csv")
    else:
        st.caption("เมื่อปิดสถานะแล้วจะแสดงกำไร/ขาดทุนสุทธิที่นี่")


def show_backtest(bars, fees, limits):
    st.subheader("ทดสอบ Opening Range Breakout")
    st.write("ใช้กรอบ 15 นาทีแรกตั้งแต่ 10:00 น. รอราคาทะลุกรอบพร้อม volume "
             "อย่างน้อย 1.5 เท่าของค่าเฉลี่ยในกรอบ แล้วเข้าที่ราคาเปิดแท่งถัดไป "
             "stop ที่กรอบล่าง เป้าหมาย 2 เท่าของระยะ stop "
             "และบังคับปิดที่ close ของแท่งสุดท้ายที่มีในวันนั้น")
    st.caption("ข้อมูลแท่งละ 5 นาที · ถือครั้งละหนึ่งสถานะ · ไม่ซื้อแล้วขายในแท่งสัญญาณเดียวกัน · "
               "หากแท่งเดียวแตะทั้ง stop และ target จะถือว่า stop เกิดก่อน · "
               "ทดสอบกับเงินจำลองใหม่ 2,000 บาท ไม่แก้ยอดพอร์ตที่บันทึกไว้")
    st.caption("หากข้อมูลวันนั้นไม่ครบ อาจจำลองปิดเร็วกว่าตลาดจริง "
               "ใช้เวลาเซสชันแบบย่อ ยังไม่มีปฏิทินวันหยุดและช่วงประมูลราคา")
    if st.button("รันทดสอบย้อนหลัง", type="primary"):
        try:
            signals = opening_range_signals(bars)
            result = backtest_signals(bars, signals, fees, limits)
            a, b, c = st.columns(3)
            a.metric("สัญญาณทั้งหมด", len(signals))
            b.metric("รายการที่เทรดได้", len(result.trades))
            c.metric("เงินปลายการทดสอบ", baht(result.ending_equity))
            if not result.trades.empty:
                equity = pd.Series([float(limits.initial_capital)] + result.trades.equity.tolist())
                drawdown = ((equity.cummax() - equity) / equity.cummax()).max() * 100
                a, b, c = st.columns(3)
                a.metric("กำไรสุทธิ", baht(result.ending_equity - limits.initial_capital))
                b.metric("รายการที่มีกำไร", f"{(result.trades.pnl > 0).mean():.0%}")
                c.metric("Drawdown ณ จุดปิดรายการ", f"{drawdown:.2f}%")
                st.caption("Drawdown นี้คำนวณจาก equity หลังปิดแต่ละรายการ "
                           "ยังไม่รวมการแกว่งของ equity ระหว่างถือสถานะ")
                st.dataframe(result.trades, hide_index=True, width="stretch")
                st.download_button("ดาวน์โหลดผลทดสอบ", result.trades.to_csv(index=False).encode("utf-8-sig"),
                                   "backtest.csv", "text/csv")
            else:
                st.info("ไม่มีรายการผ่านกติกา ผลนี้ไม่ได้ยืนยันว่ากลยุทธ์ทำกำไร")
            if not result.skipped.empty:
                st.markdown("**รายการที่ข้ามและเหตุผล**")
                st.dataframe(result.skipped, hide_index=True, width="stretch")
            st.caption("ข้อมูลตัวอย่างใช้ทดสอบระบบเท่านั้น ผลย้อนหลังไม่ใช่หลักฐานความสามารถทำกำไร "
                       "ต้องทดสอบข้อมูลจริงช่วงที่ไม่ได้ใช้พัฒนากลยุทธ์และทดลองเดินหน้าต่อ")
        except ValueError as exc:
            st.error(str(exc))


def main(*, hosted_demo=False):
    st.set_page_config(page_title="Rich Trading · BLS", page_icon="📈", layout="wide",
                       initial_sidebar_state="collapsed" if hosted_demo else "auto")
    if hosted_demo:
        # Each browser session owns its temporary journal. Never open the local
        # real-money journal, even if RICH_TRADING_DB exists on the host.
        if "_demo_workspace" not in st.session_state:
            st.session_state["_demo_workspace"] = TemporaryDirectory(prefix="rich-trading-demo-")
        store = Store(Path(st.session_state["_demo_workspace"].name) / "demo.sqlite3")
    else:
        store = Store(os.environ.get("RICH_TRADING_DB", str(ROOT / "var" / "trading.sqlite3")))
    settings = DEFAULTS | store.load_settings()
    fees = fees_from(settings)
    limits = RiskLimits(risk_per_trade=D(settings["risk_per_trade"]),
                        daily_loss_limit=D(settings["daily_loss_limit"]))
    st.sidebar.title("Rich Trading")
    st.sidebar.caption("BLS / STREAMING · CASH BALANCE")
    pages = ["ภาพรวม", "แผนเทรด", "พอร์ตจำลอง", "ทดสอบย้อนหลัง", "ตั้งค่า"]
    if not hosted_demo:
        pages.insert(3, "บันทึกเงินจริง")
    page = st.sidebar.radio("พื้นที่ทำงาน", pages)
    st.sidebar.divider()
    source = st.sidebar.radio("ข้อมูลราคา", ["ตัวอย่างสมมติ", "อัปโหลด CSV"])
    file = st.sidebar.file_uploader("ข้อมูล OHLCV ระหว่างวัน", type=["csv"]) if source == "อัปโหลด CSV" else None
    sample = ROOT / "data" / "sample_intraday.csv"
    st.sidebar.download_button("ดาวน์โหลด CSV ตัวอย่าง", sample.read_bytes(),
                               "sample_intraday.csv", "text/csv")
    st.sidebar.caption("คอลัมน์: timestamp, symbol, open, high, low, close, volume, lot_size · "
                       "เวลาต้องระบุ timezone เช่น +07:00")
    st.title("วางแผนให้ชัด ก่อนส่งคำสั่ง")
    st.caption("ผู้ช่วยวิจัยหุ้นไทย · พอร์ตจำลองและสมุดบันทึกส่วนตัว · ยังไม่เชื่อมข้อมูลหรือส่งคำสั่งกับ BLS")
    if hosted_demo:
        st.info("รุ่นทดลองผ่านเว็บ · พอร์ตและการตั้งค่าแยกสำหรับแต่ละเซสชัน "
                "ไม่มีสมุดบันทึกเงินจริง ข้อมูลอาจหายเมื่อเซสชันสิ้นสุดหรือเซิร์ฟเวอร์รีสตาร์ต "
                "ดาวน์โหลดประวัติ CSV หากต้องการเก็บผลทดลอง")
        st.caption("เปิดเมนูด้านซ้ายเพื่อเลือกหน้า บนมือถือเลื่อนตารางแนวนอนเพื่อดูรายละเอียดได้")
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))
    if not settings["fees_confirmed"]:
        st.warning("กำลังใช้ค่าธรรมเนียมสมมติ — ตรวจวิธีคิดและอัตราจริงของบัญชีในหน้าตั้งค่าก่อนนำไปประกอบการเทรด")
    if page == "ตั้งค่า":
        show_settings(store, settings, hosted_demo=hosted_demo)
        return
    # The real journal is independent of imported research data.
    if page == "บันทึกเงินจริง":
        now = datetime.now(timezone.utc)
        show_portfolio(store, "real", store.snapshot("real", as_of=now), None, fees, limits, now)
        return
    if source == "อัปโหลด CSV" and file is None:
        st.info("อัปโหลด CSV เพื่อเริ่ม หรือเลือกข้อมูลตัวอย่างสมมติ")
        return
    try:
        bars = load_bars(file if file is not None else sample)
        aligned = ((bars.timestamp.dt.minute % 5 == 0)
                   & (bars.timestamp.dt.second == 0)
                   & (bars.timestamp.dt.microsecond == 0)
                   & (bars.timestamp.dt.nanosecond == 0))
        if not aligned.all():
            raise ValueError("ต้นแบบนี้รับแท่ง 5 นาทีแบบเวลาเริ่มแท่งตรง 00/05/10/... นาทีเท่านั้น")
    except (ValueError, OSError) as exc:
        st.error(f"อ่านข้อมูลไม่ได้: {exc}")
        return
    # CSV bars are start-stamped; their close is known only five minutes later.
    as_of = bars.timestamp.max().to_pydatetime() + timedelta(minutes=5)
    name = "DEMO · หุ้นและราคาสมมติ" if source == "ตัวอย่างสมมติ" else "CSV ที่ผู้ใช้นำเข้า · ไม่ยืนยันแหล่งที่มา"
    st.info(f"{name} | สิ้นสุดแท่งล่าสุด {as_of.astimezone(TZ):%d/%m/%Y %H:%M} น. | ไม่ใช่ข้อมูลราคาสด")
    snapshot = store.snapshot("paper", as_of=as_of)
    if page == "ภาพรวม":
        show_overview(bars, snapshot, limits, name)
    elif page == "แผนเทรด":
        show_planner(store, bars, snapshot, fees, limits, as_of)
    elif page == "พอร์ตจำลอง":
        show_portfolio(store, "paper", snapshot, bars, fees, limits, as_of)
    elif page == "ทดสอบย้อนหลัง":
        show_backtest(bars, fees, limits)


if __name__ == "__main__":
    main()

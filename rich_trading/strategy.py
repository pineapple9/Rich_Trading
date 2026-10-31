"""Offline opening-range research, with conservative, single-position fills.

This simplified SET-session model uses 5-minute bars stamped at their start,
10:00–12:25 and 14:30–16:25 Asia/Bangkok. It is not a trading calendar and does
not model auctions. No claims of live data or broker execution are made.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math

import pandas as pd

from rich_trading.risk import FeeSchedule, PortfolioState, RiskLimits, plan_trade

SIGNAL_COLUMNS = ["timestamp", "symbol", "entry", "stop", "target", "lot_size", "reason"]
TRADE_COLUMNS = ["symbol", "entry_time", "exit_time", "quantity", "entry", "exit", "reason", "pnl", "equity", "entry_fee", "exit_fee"]
SKIP_COLUMNS = ["timestamp", "symbol", "reason"]
SLIPPAGE = Decimal("0.001")
SATANG = Decimal("0.01")


def _session_bars(bars: pd.DataFrame) -> pd.DataFrame:
    minutes = bars["timestamp"].dt.hour * 60 + bars["timestamp"].dt.minute
    regular = minutes.between(600, 745) | minutes.between(870, 985)
    aligned = ((bars["timestamp"].dt.minute % 5 == 0) & (bars["timestamp"].dt.second == 0)
               & (bars["timestamp"].dt.microsecond == 0) & (bars["timestamp"].dt.nanosecond == 0))
    if not aligned.all():
        raise ValueError("กลยุทธ์นี้รองรับแท่ง 5 นาทีที่ timestamp เป็นเวลาเริ่มแท่งตรงนาที 00/05/10/... เท่านั้น")
    return bars.loc[regular].copy()


def opening_range_signals(
    bars: pd.DataFrame,
    opening_minutes: int = 15,
    volume_multiple: float = 1.5,
    reward_multiple: float = 2.0,
) -> pd.DataFrame:
    """Return at most one filled-entry candidate per symbol per Bangkok day.

    The opening window must contain all 5-minute bars from 10:00. A breakout
    uses a completed close and volume. Its entry is the NEXT contiguous bar's
    open, never the breakout candle's close. The next open is an offline fill,
    not information available when the breakout first happens.
    """
    if isinstance(opening_minutes, bool) or not isinstance(opening_minutes, int) or opening_minutes < 5 or opening_minutes > 60 or opening_minutes % 5:
        raise ValueError("opening_minutes ต้องเป็นจำนวนเต็มเท่าของ 5 ระหว่าง 5–60")
    if not math.isfinite(volume_multiple) or volume_multiple <= 0 or not math.isfinite(reward_multiple) or reward_multiple <= 0:
        raise ValueError("volume_multiple และ reward_multiple ต้องมากกว่า 0 และมีค่าจำกัด")
    if bars.empty:
        return pd.DataFrame(columns=SIGNAL_COLUMNS)
    regular = _session_bars(bars)
    signals = []
    for (symbol, day), group in regular.groupby([regular["symbol"], regular["timestamp"].dt.date], sort=True):
        group = group.sort_values("timestamp").reset_index(drop=True)
        anchor = pd.Timestamp(f"{day.isoformat()}T10:00:00", tz="Asia/Bangkok")
        expected = pd.date_range(anchor, periods=opening_minutes // 5, freq="5min")
        opening = group[group["timestamp"].isin(expected)]
        if len(opening) != len(expected):
            continue
        high, low = float(opening["high"].max()), float(opening["low"].min())
        average_volume = float(opening["volume"].mean())
        if average_volume <= 0:
            continue
        window_end = pd.Timestamp(anchor.to_pydatetime() + timedelta(minutes=opening_minutes))
        for index in range(1, len(group) - 1):
            bar, previous, next_bar = group.iloc[index], group.iloc[index - 1], group.iloc[index + 1]
            if bar["timestamp"] < window_end:
                continue
            if next_bar["timestamp"].timestamp() - bar["timestamp"].timestamp() != 300:
                continue
            if not (float(previous["close"]) <= high < float(bar["close"])):
                continue
            if float(bar["volume"]) < average_volume * volume_multiple:
                continue
            entry = float(next_bar["open"])
            if entry <= low:
                continue
            signals.append({
                "timestamp": next_bar["timestamp"], "symbol": symbol,
                "entry": entry, "stop": low, "target": entry + reward_multiple * (entry - low),
                "lot_size": int(next_bar["lot_size"]),
                "reason": f"ปิดเหนือกรอบเปิด {opening_minutes} นาที พร้อมปริมาณซื้อขาย; จำลองเข้าที่ open แท่งถัดไป",
            })
            break
    return pd.DataFrame(signals, columns=SIGNAL_COLUMNS).sort_values(["timestamp", "symbol"]).reset_index(drop=True)


@dataclass(frozen=True)
class BacktestResult:
    trades: pd.DataFrame
    skipped: pd.DataFrame
    ending_equity: Decimal


def backtest_signals(bars: pd.DataFrame, signals: pd.DataFrame, fees: FeeSchedule, limits: RiskLimits) -> BacktestResult:
    """Research-only long positions, one at a time, without leverage.

    Fees on both sides and 0.1% adverse slippage per side are included. Stop
    wins when both thresholds are touched in one bar. A gap below the stop
    fills at the worse opening price. Target fills never improve on the target.
    Remaining positions close at the last available SAME-day bar, never using
    another day's close. Early-truncated uploads may thus close early; results
    describe the supplied data rather than a guaranteed exchange-close fill.
    """
    equity = Decimal(str(limits.initial_capital))
    if bars.empty or signals.empty:
        return BacktestResult(pd.DataFrame(columns=TRADE_COLUMNS), pd.DataFrame(columns=SKIP_COLUMNS), equity)
    regular = _session_bars(bars)
    trades, skipped = [], []
    busy_until = None
    active_day = None
    day_start_equity = equity
    for _, signal in signals.sort_values(["timestamp", "symbol"]).iterrows():
        stamp = pd.Timestamp(signal["timestamp"])
        symbol = str(signal["symbol"])

        def reject(reason: str) -> None:
            skipped.append({"timestamp": stamp, "symbol": symbol, "reason": reason})

        if busy_until is not None and stamp <= busy_until:
            reject("มีสถานะอื่นเปิดอยู่ในช่วงเวลานี้")
            continue
        if equity <= 0:
            reject("เงินทุนหมด ให้หยุดและทบทวนแผน")
            continue
        day = stamp.tz_convert("Asia/Bangkok").date()
        if day != active_day:
            active_day, day_start_equity = day, equity
        path = regular[(regular["symbol"] == symbol) & (regular["timestamp"].dt.date == day) & (regular["timestamp"] >= stamp)].sort_values("timestamp")
        if len(path) < 2 or path.iloc[0]["timestamp"] != stamp:
            reject("ไม่มีแท่งเข้าและแท่งถัดไปในวันเดียวกัน")
            continue
        # Never permit a caller-supplied entry to replace the executable open.
        entry = Decimal(str(path.iloc[0]["open"]))
        if int(signal["lot_size"]) != int(path.iloc[0]["lot_size"]):
            reject("lot_size ของสัญญาณไม่ตรงกับข้อมูลราคา")
            continue
        if abs(entry - Decimal(str(signal["entry"]))) > Decimal("0.00000001"):
            reject("ราคาเข้าต้องตรงกับ open ของแท่งเข้าจริง")
            continue
        stop, target = Decimal(str(signal["stop"])), Decimal(str(signal["target"]))
        if not stop < entry < target:
            reject("จุดเข้า จุดหยุด และเป้าหมายไม่สอดคล้องกัน")
            continue
        plan = plan_trade(symbol, entry, stop, target, int(signal["lot_size"]), fees, limits,
                          PortfolioState(cash=equity, equity=equity, day_start_equity=day_start_equity), slippage_rate=SLIPPAGE)
        if not plan.approved:
            reject("; ".join(plan.reasons))
            continue
        exit_bar = path.iloc[-1]
        exit_price = Decimal(str(exit_bar["close"]))
        exit_reason = "จบข้อมูลของวัน"
        for _, bar in path.iterrows():
            open_price, low_price, high_price = (Decimal(str(bar[column])) for column in ("open", "low", "high"))
            if open_price <= stop:
                exit_bar, exit_price, exit_reason = bar, open_price, "เปิดต่ำกว่าจุดตัดขาดทุน"
                break
            if low_price <= stop:
                exit_bar, exit_price, exit_reason = bar, stop, "ตัดขาดทุน"
                break
            if high_price >= target:
                exit_bar, exit_price, exit_reason = bar, target, "ถึงเป้าหมาย"
                break
        quantity = plan.quantity
        slipped_entry, slipped_exit = entry * (1 + SLIPPAGE), exit_price * (1 - SLIPPAGE)
        entry_notional = slipped_entry * quantity
        exit_notional = slipped_exit * quantity
        entry_fee, exit_fee = fees.fee(entry_notional), fees.fee(exit_notional)
        cost = (entry_notional + entry_fee).quantize(SATANG, rounding=ROUND_CEILING)
        proceeds = (exit_notional - exit_fee).quantize(SATANG, rounding=ROUND_FLOOR)
        pnl = proceeds - cost
        equity += pnl
        busy_until = exit_bar["timestamp"]
        trades.append({
            "symbol": symbol, "entry_time": stamp, "exit_time": busy_until,
            "quantity": quantity, "entry": float(slipped_entry), "exit": float(slipped_exit),
            "reason": exit_reason, "pnl": float(pnl), "equity": float(equity),
            "entry_fee": float(entry_fee), "exit_fee": float(exit_fee),
        })
    return BacktestResult(pd.DataFrame(trades, columns=TRADE_COLUMNS), pd.DataFrame(skipped, columns=SKIP_COLUMNS), equity)

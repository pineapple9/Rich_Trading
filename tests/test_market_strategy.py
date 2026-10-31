from decimal import Decimal
from datetime import timedelta
import io
from pathlib import Path

import pandas as pd
import pytest

from rich_trading.market import load_bars
from rich_trading.risk import FeeSchedule, RiskLimits
from rich_trading.strategy import backtest_signals, opening_range_signals


SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample_intraday.csv"


def csv_for(rows):
    return io.StringIO("timestamp,symbol,open,high,low,close,volume,lot_size\n" + "\n".join(rows))


def one_bar(**changes):
    record = dict(timestamp="2025-01-06T10:00:00+07:00", symbol="DEMO1", open=1, high=1.01, low=.99, close=1, volume=100, lot_size=100)
    record.update(changes)
    return ",".join(str(value) for value in record.values())


def no_fees():
    return FeeSchedule(commission_rate=Decimal("0"), other_rate=Decimal("0"), minimum_per_order=Decimal("0"), vat_rate=Decimal("0"))


def test_load_sample_and_normalize_utc():
    bars = load_bars(SAMPLE)
    assert len(bars) == 216
    assert str(bars.timestamp.dt.tz) == "Asia/Bangkok"
    assert set(bars.symbol) == {"DEMO1", "DEMO2"}
    utc = load_bars(csv_for([one_bar(timestamp="2025-01-06T03:00:00Z", symbol=" demo1 ")]))
    assert utc.iloc[0].timestamp.hour == 10
    assert utc.iloc[0].symbol == "DEMO1"


@pytest.mark.parametrize("changes", [
    {"timestamp": "2025-01-06T10:00:00"}, {"timestamp": "not-a-time"},
    {"open": "nan"}, {"volume": "inf"}, {"close": 0}, {"low": 1.02},
    {"lot_size": 0}, {"lot_size": 1.5}, {"volume": -1}, {"symbol": "=SUM(1)"},
])
def test_reject_bad_data(changes):
    with pytest.raises(ValueError):
        load_bars(csv_for([one_bar(**changes)]))


def test_reject_duplicates_and_inconsistent_lots():
    with pytest.raises(ValueError, match="ซ้ำ"):
        load_bars(csv_for([one_bar(), one_bar()]))
    with pytest.raises(ValueError, match="lot_size"):
        load_bars(csv_for([one_bar(), one_bar(timestamp="2025-01-06T10:05:00+07:00", lot_size=50)]))


def test_signals_use_next_open_and_no_future_high_or_volume():
    bars = load_bars(SAMPLE)
    one_day = bars[(bars.symbol == "DEMO1") & (bars.timestamp.dt.day == 6)].copy()
    signals = opening_range_signals(one_day)
    assert len(signals) == 1
    signal = signals.iloc[0]
    assert signal.timestamp.strftime("%H:%M") == "10:20"
    assert signal.entry == pytest.approx(1.03)
    assert signal.stop == pytest.approx(.99)
    before_entry = one_day[one_day.timestamp < signal.timestamp]
    assert opening_range_signals(before_entry).empty
    through_entry = one_day[one_day.timestamp <= signal.timestamp]
    pd.testing.assert_frame_equal(opening_range_signals(through_entry), signals)
    changed = one_day.copy()
    changed.loc[changed.timestamp > signal.timestamp, ["high", "volume"]] = [1000, 1e12]
    pd.testing.assert_frame_equal(opening_range_signals(changed), signals)


def test_missing_opening_bar_or_zero_volume_produces_no_signal():
    bars = load_bars(SAMPLE)
    one_day = bars[(bars.symbol == "DEMO1") & (bars.timestamp.dt.day == 6)]
    assert opening_range_signals(one_day.iloc[1:]).empty
    zero = one_day.copy()
    zero.loc[zero.timestamp.dt.hour == 10, "volume"] = 0
    assert opening_range_signals(zero).empty


def test_one_minute_bars_are_not_silently_treated_as_five_minute_bars():
    bars = load_bars(csv_for([one_bar(timestamp="2025-01-06T10:01:00+07:00")]))
    with pytest.raises(ValueError, match="5 นาที"):
        opening_range_signals(bars)


def path_and_signal(next_open=1.0, next_low=.99, next_high=1.02, last_close=1.01):
    bars = load_bars(csv_for([
        one_bar(timestamp="2025-01-06T10:20:00+07:00", low=.99, high=1.01),
        one_bar(timestamp="2025-01-06T10:25:00+07:00", open=next_open, low=next_low, high=next_high, close=next_open),
        one_bar(timestamp="2025-01-06T10:30:00+07:00", open=last_close, close=last_close, low=min(last_close,.99), high=max(last_close,1.02)),
    ]))
    signals = pd.DataFrame([dict(timestamp=bars.iloc[0].timestamp, symbol="DEMO1", entry=1., stop=.95, target=1.15, lot_size=100, reason="test")])
    return bars, signals


def test_stop_first_on_ambiguous_candle_and_cash_accounting():
    bars, signals = path_and_signal(next_low=.94, next_high=1.16)
    result = backtest_signals(bars, signals, no_fees(), RiskLimits())
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert trade.reason == "ตัดขาดทุน"
    assert trade["exit"] == pytest.approx(.95 * .999)
    assert trade.entry == pytest.approx(1.001)
    assert result.ending_equity == Decimal("2000") + Decimal(str(trade.pnl))
    assert trade.quantity % 100 == 0


def test_gap_below_stop_fills_worse_open():
    bars, signals = path_and_signal(next_open=.90, next_low=.89, next_high=.92)
    result = backtest_signals(bars, signals, no_fees(), RiskLimits())
    assert result.trades.iloc[0].reason == "เปิดต่ำกว่าจุดตัดขาดทุน"
    assert result.trades.iloc[0]["exit"] == pytest.approx(.90 * .999)


def test_same_day_end_of_data_exit_and_no_next_day_price():
    bars, signals = path_and_signal()
    tomorrow = bars.copy()
    tomorrow["timestamp"] = tomorrow["timestamp"].map(lambda stamp: pd.Timestamp(stamp.to_pydatetime() + timedelta(days=1)))
    tomorrow[["open", "high", "low", "close"]] *= 10
    result = backtest_signals(pd.concat([bars, tomorrow]), signals, no_fees(), RiskLimits())
    trade = result.trades.iloc[0]
    assert trade.exit_time.date() == trade.entry_time.date()
    assert trade.reason == "จบข้อมูลของวัน"
    assert trade["exit"] == pytest.approx(1.01 * .999)


def test_no_last_bar_entry_and_no_overlapping_positions():
    bars, signals = path_and_signal()
    duplicate = signals.copy()
    overlapping = pd.concat([signals, duplicate])
    result = backtest_signals(bars, overlapping, no_fees(), RiskLimits())
    assert len(result.trades) == 1
    assert "สถานะอื่น" in result.skipped.iloc[0].reason
    final = signals.copy()
    final["timestamp"] = bars.iloc[-1].timestamp
    final["entry"] = bars.iloc[-1].open
    result = backtest_signals(bars, final, no_fees(), RiskLimits())
    assert result.trades.empty
    assert len(result.skipped) == 1


def test_fees_can_make_small_account_untradable():
    bars = load_bars(SAMPLE)
    signals = opening_range_signals(bars)
    result = backtest_signals(bars, signals, FeeSchedule(), RiskLimits())
    assert result.trades.empty
    assert len(result.skipped) == len(signals) == 4
    assert result.ending_equity == Decimal("2000")
    low_cost = backtest_signals(bars, signals, no_fees(), RiskLimits())
    assert not low_cost.trades.empty
    assert low_cost.ending_equity > Decimal("2000")

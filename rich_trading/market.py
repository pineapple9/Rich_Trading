"""Validated CSV boundary for intraday OHLCV data.

Bar timestamps identify the START of a bar. CSV files are limited to 10 MiB and
100,000 rows. A source must supply an explicit UTC offset on every timestamp;
we never guess the timezone of uploaded prices.
"""

from __future__ import annotations

import io
from pathlib import Path
import re
from typing import IO

import numpy as np
import pandas as pd

TIMEZONE = "Asia/Bangkok"
MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 100_000
COLUMNS = ["timestamp", "symbol", "open", "high", "low", "close", "volume", "lot_size"]
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,19}\Z")


def load_bars(source: str | Path | IO) -> pd.DataFrame:
    """Read and validate prices, returning chronological, Bangkok-aware bars.

    `lot_size` comes from the source rather than assuming every SET security
    has the same board lot. Additional CSV columns are ignored.
    """
    if isinstance(source, (str, Path)):
        with Path(source).open("rb") as handle:
            content = handle.read(MAX_BYTES + 1)
    else:
        content = source.read(MAX_BYTES + 1)
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not content or len(content) > MAX_BYTES:
        raise ValueError("ไฟล์ CSV ต้องไม่ว่างและมีขนาดไม่เกิน 10 MiB")
    try:
        bars = pd.read_csv(io.BytesIO(content), dtype={"timestamp": "string", "symbol": "string"}, nrows=MAX_ROWS + 1)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeError) as exc:
        raise ValueError("อ่านไฟล์ CSV ไม่ได้ กรุณาใช้ไฟล์ UTF-8 ตามตัวอย่าง") from exc
    missing = set(COLUMNS) - set(bars.columns)
    if missing:
        raise ValueError("CSV ขาดคอลัมน์: " + ", ".join(sorted(missing)))
    if bars.empty or len(bars) > MAX_ROWS:
        raise ValueError("CSV ต้องมีข้อมูล 1–100,000 แถว")
    bars = bars[COLUMNS].copy()
    if bars["timestamp"].isna().any() or bars["symbol"].isna().any():
        raise ValueError("timestamp และ symbol ต้องไม่ว่าง")

    timestamps = []
    for value in bars["timestamp"]:
        try:
            stamp = pd.Timestamp(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("timestamp ไม่ถูกต้อง ต้องเป็น ISO 8601 พร้อมเขตเวลา") from exc
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError("timestamp ทุกแถวต้องระบุเขตเวลา เช่น 2025-01-06T10:00:00+07:00")
        timestamps.append(stamp)
    try:
        bars["timestamp"] = pd.to_datetime(timestamps, utc=True).tz_convert(TIMEZONE)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("timestamp อยู่นอกช่วงวันที่ที่รองรับ") from exc
    bars["symbol"] = bars["symbol"].str.strip().str.upper()
    if not bars["symbol"].map(lambda symbol: bool(_SYMBOL.fullmatch(symbol))).all():
        raise ValueError("symbol ต้องเป็นอักษรอังกฤษ/ตัวเลข . _ - ความยาว 1–20 ตัว")
    for name in COLUMNS[2:]:
        bars[name] = pd.to_numeric(bars[name], errors="coerce")
        if not np.isfinite(bars[name].to_numpy(dtype=float)).all():
            raise ValueError(f"{name} ต้องเป็นตัวเลขที่มีค่าจำกัดทุกแถว")
    if (bars[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("ราคาต้องมากกว่า 0")
    if (bars["volume"] < 0).any():
        raise ValueError("volume ต้องไม่ติดลบ")
    if ((bars["lot_size"] <= 0) | (bars["lot_size"] % 1 != 0) | (bars["lot_size"] > 1_000_000)).any():
        raise ValueError("lot_size ต้องเป็นจำนวนเต็ม 1–1,000,000")
    bars["lot_size"] = bars["lot_size"].astype(int)
    invalid_bounds = (
        (bars["low"] > bars[["open", "close"]].min(axis=1))
        | (bars["high"] < bars[["open", "close"]].max(axis=1))
        | (bars["high"] < bars["low"])
    )
    if invalid_bounds.any():
        raise ValueError("OHLC ไม่สอดคล้องกัน: low ≤ open/close ≤ high")
    if bars.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("พบ symbol และ timestamp ซ้ำ")
    if (bars.groupby("symbol")["lot_size"].nunique() != 1).any():
        raise ValueError("lot_size ของหุ้นเดียวกันต้องสอดคล้องกัน")
    return bars.sort_values(["timestamp", "symbol"]).reset_index(drop=True)

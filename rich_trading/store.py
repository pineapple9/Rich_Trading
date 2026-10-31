"""Local, transactional journals for simulated and manually recorded trades.

This module never communicates with a broker. Quotes and fills are event-timed,
so historical snapshots cannot see future prices or completed trades. Fills must
be recorded chronologically within each ledger to preserve a cash-only balance.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator
from zoneinfo import ZoneInfo

from .risk import FeeSchedule, PortfolioState, RiskLimits, plan_trade


ZERO = Decimal("0")
DEFAULT_CAPITAL = Decimal("2000")
BANGKOK = ZoneInfo("Asia/Bangkok")
UTC = timezone.utc


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    day_start_equity: Decimal
    open_risk: Decimal
    marks_valid: bool
    positions: list[dict]
    trades: list[dict]
    equity_history: list[dict] = field(default_factory=list)


def _amount(value, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'} and finite")
    return amount


def _ledger(value: str) -> str:
    if value not in {"paper", "real"}:
        raise ValueError("ledger must be 'paper' or 'real'")
    return value


def _symbol(value: str) -> str:
    symbol = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", symbol):
        raise ValueError("symbol must contain 1–32 ASCII ticker characters")
    return symbol


def _datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware datetime values")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _datetime(value).isoformat(timespec="microseconds")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Store:
    """SQLite-backed cash portfolios; a new connection is used per operation."""

    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("Store requires a filesystem path for persistence")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY,
                    ledger TEXT NOT NULL CHECK (ledger IN ('paper', 'real')),
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    entry_price TEXT NOT NULL,
                    entry_fee TEXT NOT NULL,
                    stop TEXT NOT NULL,
                    target TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    reference TEXT NOT NULL DEFAULT '',
                    exit_price TEXT,
                    exit_fee TEXT,
                    closed_at TEXT,
                    close_notes TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS position_ledger_time
                    ON positions (ledger, opened_at);
                CREATE UNIQUE INDEX IF NOT EXISTS unique_trade_reference
                    ON positions (ledger, reference) WHERE reference <> '';
                CREATE TABLE IF NOT EXISTS quotes (
                    ledger TEXT NOT NULL CHECK (ledger IN ('paper', 'real')),
                    symbol TEXT NOT NULL,
                    price TEXT NOT NULL,
                    marked_at TEXT NOT NULL,
                    PRIMARY KEY (ledger, symbol, marked_at)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    data TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _check_sequence(conn: sqlite3.Connection, ledger: str, timestamp: str) -> None:
        row = conn.execute(
            """SELECT MAX(event_at) AS latest FROM (
                   SELECT opened_at AS event_at FROM positions WHERE ledger = ?
                   UNION ALL
                   SELECT closed_at AS event_at FROM positions
                     WHERE ledger = ? AND closed_at IS NOT NULL
               )""",
            (ledger, ledger),
        ).fetchone()
        if row["latest"] is not None and timestamp < row["latest"]:
            raise ValueError("Record fills chronologically within each ledger")

    @staticmethod
    def _cash(conn: sqlite3.Connection, ledger: str, initial_capital: Decimal,
              timestamp: str, *, inclusive: bool = True) -> Decimal:
        comparison = "<=" if inclusive else "<"
        rows = conn.execute(
            f"SELECT * FROM positions WHERE ledger = ? AND opened_at {comparison} ?",
            (ledger, timestamp),
        ).fetchall()
        cash = initial_capital
        for row in rows:
            quantity = row["quantity"]
            cash -= Decimal(row["entry_price"]) * quantity + Decimal(row["entry_fee"])
            if row["closed_at"] is not None and (
                row["closed_at"] <= timestamp if inclusive else row["closed_at"] < timestamp
            ):
                cash += Decimal(row["exit_price"]) * quantity - Decimal(row["exit_fee"])
        return cash

    def buy(self, ledger: str, symbol: str, quantity: int, entry_price,
            entry_fee, stop, target, opened_at: datetime, notes: str = "",
            reference: str = "", *, initial_capital=DEFAULT_CAPITAL) -> int:
        ledger, symbol = _ledger(ledger), _symbol(symbol)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        price = _amount(entry_price, "entry_price", positive=True)
        fee = _amount(entry_fee, "entry_fee")
        stop_price = _amount(stop, "stop", positive=True)
        target_price = _amount(target, "target", positive=True)
        capital = _amount(initial_capital, "initial_capital", positive=True)
        if not stop_price < price < target_price:
            raise ValueError("long-only trades require stop < entry_price < target")
        if not isinstance(notes, str) or len(notes) > 5000:
            raise ValueError("notes must be text with at most 5000 characters")
        if not isinstance(reference, str) or len(reference) > 128:
            raise ValueError("reference must be text with at most 128 characters")
        timestamp = _iso(opened_at)
        with self._connection(write=True) as conn:
            self._check_sequence(conn, ledger, timestamp)
            cash = self._cash(conn, ledger, capital, timestamp)
            if price * quantity + fee > cash:
                raise ValueError("Insufficient cash: this Cash Balance journal does not allow borrowing")
            try:
                cursor = conn.execute(
                    """INSERT INTO positions
                       (ledger, symbol, quantity, entry_price, entry_fee, stop, target,
                        opened_at, notes, reference) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ledger, symbol, quantity, str(price), str(fee), str(stop_price),
                     str(target_price), timestamp, notes, reference.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("A trade with this reference already exists in this ledger") from exc
            return int(cursor.lastrowid)

    def close(self, position_id: int, exit_price, exit_fee,
              closed_at: datetime, notes: str = "", *, initial_capital=DEFAULT_CAPITAL) -> None:
        price = _amount(exit_price, "exit_price", positive=True)
        fee = _amount(exit_fee, "exit_fee")
        capital = _amount(initial_capital, "initial_capital", positive=True)
        timestamp = _iso(closed_at)
        if not isinstance(notes, str) or len(notes) > 5000:
            raise ValueError("notes must be text with at most 5000 characters")
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
            if row is None:
                raise ValueError("Position does not exist")
            if row["closed_at"] is not None:
                raise ValueError("Position is already closed")
            if timestamp < row["opened_at"]:
                raise ValueError("Exit time cannot precede entry time")
            self._check_sequence(conn, row["ledger"], timestamp)
            cash = self._cash(conn, row["ledger"], capital, timestamp)
            if cash + price * row["quantity"] - fee < ZERO:
                raise ValueError("Insufficient cash to pay the exit fee")
            conn.execute(
                """UPDATE positions SET exit_price = ?, exit_fee = ?, closed_at = ?,
                   close_notes = ? WHERE id = ?""",
                (str(price), str(fee), timestamp, notes, position_id),
            )

    def buy_paper_plan(self, symbol: str, entry, stop, target, lot_size: int,
                       fees: FeeSchedule, limits: RiskLimits, as_of: datetime,
                       slippage_rate=Decimal("0.001")) -> int:
        """Recheck risk and record a simulated fill within one write transaction.

        This is the execution entry point for paper plans. ``buy`` remains a
        historical-fill journal API, without trading-risk authorization. Full
        original entry-to-stop risk is reserved for every open paper position,
        including stored entry costs and current estimated exit fees/slippage.
        Reference quotes must be at most 15 minutes old at the simulated clock.
        """
        symbol = _symbol(symbol)
        current = _datetime(as_of)
        timestamp = _iso(current)
        if not isinstance(fees, FeeSchedule) or not isinstance(limits, RiskLimits):
            raise ValueError("ต้องระบุค่าธรรมเนียมและขีดจำกัดความเสี่ยงที่ถูกต้อง")
        slip = _amount(slippage_rate, "slippage_rate")
        if slip >= 1:
            raise ValueError("slippage_rate must be less than one")
        with self._connection(write=True) as conn:
            self._check_sequence(conn, "paper", timestamp)
            snapshot = self._snapshot(conn, "paper", limits.initial_capital, current)
            marks_valid = snapshot.marks_valid
            reserved_risk = ZERO
            for position in snapshot.positions:
                marked_at = position["marked_at"]
                if marked_at is None or current - marked_at > timedelta(minutes=15):
                    marks_valid = False
                stop_notional = position["stop"] * (Decimal("1") - slip) * position["quantity"]
                stop_proceeds = (stop_notional - fees.fee(stop_notional)).quantize(
                    Decimal("0.01"), rounding=ROUND_FLOOR,
                )
                original_cost = position["entry_price"] * position["quantity"] + position["entry_fee"]
                reserved_risk += max(original_cost - stop_proceeds, ZERO)
            state = PortfolioState(
                cash=snapshot.cash, equity=snapshot.equity,
                day_start_equity=snapshot.day_start_equity,
                open_risk=reserved_risk, marks_valid=marks_valid,
            )
            plan = plan_trade(symbol, entry, stop, target, lot_size, fees, limits, state, slip)
            if not plan.approved:
                raise ValueError("; ".join(plan.reasons))
            fill = plan.entry * (Decimal("1") + slip)
            # Keep total cash debited exactly equal to the planner's cent-rounded
            # cost; the fee includes any sub-satang rounding adjustment.
            entry_fee = plan.total_cost - fill * plan.quantity
            cursor = conn.execute(
                """INSERT INTO positions
                   (ledger, symbol, quantity, entry_price, entry_fee, stop, target,
                    opened_at, notes, reference) VALUES ('paper', ?, ?, ?, ?, ?, ?, ?, ?, '')""",
                (symbol, plan.quantity, str(fill), str(entry_fee), str(plan.stop),
                 str(plan.target), timestamp, "Paper plan: adverse entry slippage and estimated fees"),
            )
            conn.execute(
                """INSERT INTO quotes (ledger, symbol, price, marked_at) VALUES ('paper', ?, ?, ?)
                   ON CONFLICT (ledger, symbol, marked_at) DO UPDATE SET price = excluded.price""",
                (symbol, str(plan.entry), timestamp),
            )
            return int(cursor.lastrowid)

    def mark(self, ledger: str, symbol: str, price, marked_at: datetime) -> None:
        ledger, symbol = _ledger(ledger), _symbol(symbol)
        amount = _amount(price, "price", positive=True)
        timestamp = _iso(marked_at)
        with self._connection(write=True) as conn:
            conn.execute(
                """INSERT INTO quotes (ledger, symbol, price, marked_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT (ledger, symbol, marked_at) DO UPDATE SET price = excluded.price""",
                (ledger, symbol, str(amount), timestamp),
            )

    @staticmethod
    def _latest_quote(conn: sqlite3.Connection, ledger: str, symbol: str,
                      timestamp: str, *, inclusive: bool = True):
        comparison = "<=" if inclusive else "<"
        return conn.execute(
            f"""SELECT price, marked_at FROM quotes
                WHERE ledger = ? AND symbol = ? AND marked_at {comparison} ?
                ORDER BY marked_at DESC LIMIT 1""",
            (ledger, symbol, timestamp),
        ).fetchone()

    def snapshot(self, ledger: str, initial_capital=DEFAULT_CAPITAL,
                 as_of: datetime | None = None) -> PortfolioSnapshot:
        ledger = _ledger(ledger)
        capital = _amount(initial_capital, "initial_capital", positive=True)
        current = _datetime(as_of if as_of is not None else datetime.now(UTC))
        with self._connection() as conn:
            return self._snapshot(conn, ledger, capital, current)

    def _snapshot(self, conn: sqlite3.Connection, ledger: str, capital: Decimal,
                  current: datetime) -> PortfolioSnapshot:
        """Read a consistent snapshot using the caller's existing transaction."""
        timestamp = _iso(current)
        midnight = datetime.combine(current.astimezone(BANGKOK).date(), time.min, BANGKOK)
        midnight_text = _iso(midnight)
        today = current.astimezone(BANGKOK).date()
        rows = conn.execute(
            "SELECT * FROM positions WHERE ledger = ? AND opened_at <= ? ORDER BY opened_at, id",
            (ledger, timestamp),
        ).fetchall()
        cash = self._cash(conn, ledger, capital, timestamp)
        day_equity = self._cash(conn, ledger, capital, midnight_text, inclusive=False)
        positions, trades = [], []
        realized, unrealized, market_value, risk = ZERO, ZERO, ZERO, ZERO
        marks_valid = True
        for row in rows:
            item = dict(row)
            for name in ("entry_price", "entry_fee", "stop", "target", "exit_price", "exit_fee"):
                item[name] = Decimal(item[name]) if item[name] is not None else None
            item["opened_at"] = _parse(row["opened_at"])
            item["closed_at"] = _parse(row["closed_at"]) if row["closed_at"] else None
            quantity = row["quantity"]
            cost = item["entry_price"] * quantity + item["entry_fee"]
            # A fill completed later must still appear open in an earlier snapshot.
            is_closed = row["closed_at"] is not None and row["closed_at"] <= timestamp
            if is_closed:
                item["pnl"] = item["exit_price"] * quantity - item["exit_fee"] - cost
                realized += item["pnl"]
                trades.append(item)
            else:
                item["exit_price"] = item["exit_fee"] = item["closed_at"] = None
                item["close_notes"] = ""
                quote = self._latest_quote(conn, ledger, row["symbol"], timestamp)
                usable = quote is not None and quote["marked_at"] >= row["opened_at"]
                mark_price = Decimal(quote["price"]) if usable else item["entry_price"]
                marked_at = _parse(quote["marked_at"]) if usable else None
                current_mark = marked_at is not None and marked_at.astimezone(BANGKOK).date() == today
                marks_valid = marks_valid and current_mark
                item.update(mark_price=mark_price, marked_at=marked_at,
                            unrealized_pnl=mark_price * quantity - cost,
                            mark_valid=current_mark)
                positions.append(item)
                unrealized += item["unrealized_pnl"]
                market_value += mark_price * quantity
                # Exit fees/slippage are added by the risk planner using its fee schedule.
                risk += max(item["entry_price"] - item["stop"], ZERO) * quantity + item["entry_fee"]
            # Opening equity must value carried inventory, not cash alone.
            carried = row["opened_at"] < midnight_text and (
                row["closed_at"] is None or row["closed_at"] >= midnight_text
            )
            if carried:
                prior = self._latest_quote(conn, ledger, row["symbol"], midnight_text, inclusive=False)
                if prior is None or prior["marked_at"] < row["opened_at"]:
                    prior_price = item["entry_price"]
                    marks_valid = False
                else:
                    prior_price = Decimal(prior["price"])
                day_equity += prior_price * quantity
        return PortfolioSnapshot(
            cash=cash, equity=cash + market_value, realized_pnl=realized,
            unrealized_pnl=unrealized, day_start_equity=day_equity,
            open_risk=risk, marks_valid=marks_valid,
            positions=positions, trades=trades,
        )

    def save_settings(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("settings must be a dictionary")
        try:
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("settings must be JSON-serializable without non-finite values") from exc
        with self._connection(write=True) as conn:
            conn.execute(
                "INSERT INTO settings (id, data) VALUES (1, ?) "
                "ON CONFLICT (id) DO UPDATE SET data = excluded.data", (payload,),
            )

    def load_settings(self) -> dict:
        with self._connection() as conn:
            row = conn.execute("SELECT data FROM settings WHERE id = 1").fetchone()
            return json.loads(row["data"]) if row else {}

"""Long-only, cash-funded position sizing using Decimal amounts in Thai baht.

The default fee schedule is an illustrative scenario, NOT a BLS quotation.
Minimum commission is modeled per order. Account-specific daily minimums or
different VAT rules must be represented separately before real-money use.
Prices supplied to ``plan_trade`` are reference prices; its costs account for
adverse slippage on entry and exit. A stop estimates risk, not a guaranteed fill.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR


CENT = Decimal("0.01")
ZERO = Decimal("0")
MIN_REWARD_RISK = Decimal("1.5")


def D(value: object) -> Decimal:
    """Convert a finite financial number without introducing float artifacts."""
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("จำนวนเงินต้องเป็นตัวเลขที่ถูกต้อง") from exc
    if not amount.is_finite():
        raise ValueError("จำนวนเงินต้องเป็นตัวเลข finite ห้าม NaN หรือ Infinity")
    return amount


def _up(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_CEILING)


def _down(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_FLOOR)


def _field(instance: object, name: str, *, positive: bool = False,
           maximum: Decimal | None = None) -> Decimal:
    value = D(getattr(instance, name))
    if value < ZERO or (positive and value == ZERO):
        raise ValueError(f"{name} ต้อง{'มากกว่าศูนย์' if positive else 'ไม่ติดลบ'}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} ต้องไม่เกิน {maximum}")
    object.__setattr__(instance, name, value)
    return value


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Illustrative per-order commission plus other fees and subtotal VAT."""

    commission_rate: Decimal = Decimal("0.0015")
    other_rate: Decimal = ZERO
    minimum_per_order: Decimal = Decimal("20")
    vat_rate: Decimal = Decimal("0.07")

    def __post_init__(self) -> None:
        for name in ("commission_rate", "other_rate", "vat_rate"):
            _field(self, name, maximum=Decimal("1"))
        _field(self, "minimum_per_order")

    def fee(self, notional: Decimal) -> Decimal:
        amount = D(notional)
        if amount < ZERO:
            raise ValueError("notional ต้องไม่ติดลบ")
        if amount == ZERO:
            return ZERO.quantize(CENT)
        commission = max(amount * self.commission_rate, self.minimum_per_order)
        subtotal = commission + amount * self.other_rate
        return _up(subtotal * (Decimal("1") + self.vat_rate))


@dataclass(frozen=True, slots=True)
class RiskLimits:
    initial_capital: Decimal = Decimal("2000")
    risk_per_trade: Decimal = Decimal("20")
    daily_loss_limit: Decimal = Decimal("40")
    total_loss_fraction: Decimal = Decimal("0.30")

    def __post_init__(self) -> None:
        for name in ("initial_capital", "risk_per_trade", "daily_loss_limit"):
            _field(self, name, positive=True)
        _field(self, "total_loss_fraction", positive=True, maximum=Decimal("1"))


@dataclass(frozen=True, slots=True)
class PortfolioState:
    cash: Decimal
    equity: Decimal
    day_start_equity: Decimal
    open_risk: Decimal = ZERO
    marks_valid: bool = True

    def __post_init__(self) -> None:
        for name in ("cash", "equity", "day_start_equity", "open_risk"):
            _field(self, name)
        if not isinstance(self.marks_valid, bool):
            raise ValueError("marks_valid ต้องเป็น boolean")


@dataclass(frozen=True, slots=True)
class TradePlan:
    approved: bool
    symbol: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    quantity: int
    entry_fee: Decimal
    exit_fee: Decimal
    total_cost: Decimal
    risk: Decimal
    net_reward: Decimal
    reward_risk: Decimal
    reasons: tuple[str, ...]


def plan_trade(
    symbol: str,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    lot_size: int,
    fees: FeeSchedule,
    limits: RiskLimits,
    state: PortfolioState,
    slippage_rate: Decimal = Decimal("0.001"),
) -> TradePlan:
    """Find the largest affordable lot that fits remaining loss budgets.

    Open-position risk reserves all remaining loss budgets conservatively.
    Daily drawdown includes unrealized P&L through ``state.equity``. The total
    limit is measured against initial capital, not the portfolio high-water mark.
    ``approved=False`` must never be interpreted as permission to execute, even
    if a rejected reward/risk preview includes a nonzero quantity.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol ต้องไม่ว่าง")
    symbol = symbol.strip().upper()
    entry, stop, target, slip = map(D, (entry, stop, target, slippage_rate))
    if not (ZERO < stop < entry < target):
        raise ValueError("ต้องมี 0 < stop < entry < target สำหรับสถานะซื้อ")
    if isinstance(lot_size, bool) or not isinstance(lot_size, int) or lot_size <= 0:
        raise ValueError("lot_size ต้องเป็นจำนวนเต็มบวก")
    if not ZERO <= slip < Decimal("1"):
        raise ValueError("slippage_rate ต้องอยู่ในช่วง 0 ถึงน้อยกว่า 1")
    if not isinstance(fees, FeeSchedule) or not isinstance(limits, RiskLimits):
        raise ValueError("ต้องระบุ FeeSchedule และ RiskLimits ที่ถูกต้อง")
    if not isinstance(state, PortfolioState):
        raise ValueError("ต้องระบุ PortfolioState ที่ถูกต้อง")

    def blocked(reasons: list[str]) -> TradePlan:
        return TradePlan(False, symbol, entry, stop, target, 0,
                         ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, tuple(reasons))

    day_loss = state.day_start_equity - state.equity
    total_loss = limits.initial_capital - state.equity
    total_limit = limits.initial_capital * limits.total_loss_fraction
    reasons = []
    if not state.marks_valid:
        reasons.append("ข้อมูลราคาประเมินพอร์ตไม่ครบหรือเก่าเกินไป กรุณาอัปเดตราคาก่อน")
    if day_loss >= limits.daily_loss_limit:
        reasons.append("ถึงขีดจำกัดขาดทุนรายวัน ให้หยุดเปิดสถานะใหม่")
    if total_loss >= total_limit:
        reasons.append("ถึงขีดจำกัดขาดทุนรวม ให้หยุดและทบทวนแผน")
    if reasons:
        return blocked(reasons)

    budget = min(
        limits.risk_per_trade - state.open_risk,
        limits.daily_loss_limit - day_loss - state.open_risk,
        total_limit - total_loss - state.open_risk,
    )
    if budget <= ZERO:
        return blocked(["งบความเสี่ยงคงเหลือไม่พอ หลังสำรองความเสี่ยงสถานะที่เปิดอยู่"])

    entry_fill = entry * (Decimal("1") + slip)
    stop_fill = stop * (Decimal("1") - slip)
    target_fill = target * (Decimal("1") - slip)

    def estimates(quantity: int) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        entry_notional = entry_fill * quantity
        stop_notional = stop_fill * quantity
        entry_fee = fees.fee(entry_notional)
        exit_fee = fees.fee(stop_notional)
        cost = _up(entry_notional + entry_fee)
        risk = cost - _down(stop_notional - exit_fee)
        target_notional = target_fill * quantity
        reward = _down(target_notional - fees.fee(target_notional)) - cost
        return entry_fee, exit_fee, cost, risk, reward

    # Search whole lots. Costs and stop-loss risk are monotonic in quantity.
    # This avoids iterating over every share when entry prices are very small.
    low = 0
    high = int(state.cash // (entry_fill * lot_size))
    while low < high:
        middle = (low + high + 1) // 2
        _, _, cost, risk, _ = estimates(middle * lot_size)
        if cost <= state.cash and risk <= budget:
            low = middle
        else:
            high = middle - 1
    quantity = low * lot_size
    if quantity == 0:
        return blocked(["เงินสดหรืองบความเสี่ยงไม่พอสำหรับหนึ่ง lot เมื่อรวมค่าธรรมเนียมและ slippage"])

    entry_fee, exit_fee, cost, risk, reward = estimates(quantity)
    ratio = reward / risk if risk > ZERO else ZERO
    if reward <= ZERO:
        reasons.append("ผลตอบแทนที่เป้าหมายไม่เหลือกำไรสุทธิหลังต้นทุน")
    elif ratio < MIN_REWARD_RISK:
        reasons.append("ผลตอบแทนสุทธิต่อความเสี่ยงต่ำกว่าเกณฑ์ 1.5 เท่า")
    return TradePlan(not reasons, symbol, entry, stop, target, quantity,
                     entry_fee, exit_fee, cost, risk, reward, ratio, tuple(reasons))

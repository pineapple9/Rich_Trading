"""Behavioral checks for cash, lot sizes, fees, and portfolio loss limits."""

import unittest
from decimal import Decimal

from rich_trading.risk import FeeSchedule, PortfolioState, RiskLimits, plan_trade


D = Decimal
NO_FEES = FeeSchedule(D("0"), D("0"), D("0"), D("0"))


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.limits = RiskLimits()
        self.state = PortfolioState(D("2000"), D("2000"), D("2000"))

    def plan(self, **overrides):
        values = dict(symbol="TEST", entry=D("10"), stop=D("9.90"),
                      target=D("10.30"), lot_size=100, fees=NO_FEES,
                      limits=self.limits, state=self.state, slippage_rate=D("0"))
        values.update(overrides)
        return plan_trade(**values)

    def test_cash_and_risk_can_both_be_exactly_at_limit(self):
        plan = self.plan()
        self.assertTrue(plan.approved)
        self.assertEqual(plan.quantity, 200)
        self.assertEqual(plan.total_cost, D("2000.00"))
        self.assertEqual(plan.risk, D("20.00"))
        self.assertEqual(plan.net_reward, D("60.00"))
        self.assertEqual(plan.reward_risk, D("3"))

    def test_fees_have_minimum_vat_and_round_up(self):
        fees = FeeSchedule(D("0.0015"), D("0.00001"), D("20"), D("0.07"))
        self.assertEqual(fees.fee(D("1000")), D("21.42"))
        self.assertEqual(fees.fee(D("0")), D("0"))
        fees = FeeSchedule(D("0.0015"), D("0"), D("0"), D("0.07"))
        self.assertEqual(fees.fee(D("1")), D("0.01"))

    def test_default_minimum_fees_exhaust_small_account_risk(self):
        plan = self.plan(fees=FeeSchedule())
        self.assertFalse(plan.approved)
        self.assertEqual(plan.quantity, 0)
        self.assertTrue(plan.reasons)

    def test_slippage_on_both_sides_reduces_position_size(self):
        plan = self.plan(slippage_rate=D("0.001"))
        self.assertTrue(plan.approved)
        self.assertEqual(plan.quantity, 100)
        self.assertEqual(plan.total_cost, D("1001.00"))
        self.assertEqual(plan.risk, D("11.99"))
        self.assertEqual(plan.net_reward, D("27.97"))

    def test_lot_rounding_and_cash_reject_unaffordable_lot(self):
        plan = self.plan(entry=D("30"), stop=D("29.90"), target=D("31"))
        self.assertFalse(plan.approved)
        self.assertEqual(plan.quantity, 0)
        odd = self.plan(entry=D("30"), stop=D("29.90"), target=D("31"), lot_size=1)
        self.assertTrue(odd.approved)
        self.assertEqual(odd.quantity, 66)

    def test_daily_drawdown_blocks_at_equality_including_unrealized(self):
        plan = self.plan(state=PortfolioState(D("1960"), D("1960"), D("2000")))
        self.assertFalse(plan.approved)
        self.assertTrue(any("รายวัน" in reason for reason in plan.reasons))

    def test_total_drawdown_blocks_at_equality_even_on_new_day(self):
        plan = self.plan(state=PortfolioState(D("1400"), D("1400"), D("1400")))
        self.assertFalse(plan.approved)
        self.assertTrue(any("ขาดทุนรวม" in reason for reason in plan.reasons))

    def test_remaining_daily_budget_caps_new_position(self):
        plan = self.plan(state=PortfolioState(D("1965"), D("1965"), D("2000")), lot_size=1)
        self.assertTrue(plan.approved)
        self.assertEqual(plan.quantity, 50)
        self.assertEqual(plan.risk, D("5.00"))

    def test_remaining_total_budget_caps_new_position(self):
        plan = self.plan(state=PortfolioState(D("1405"), D("1405"), D("1405")), lot_size=1)
        self.assertTrue(plan.approved)
        self.assertEqual(plan.risk, D("5.00"))

    def test_existing_open_risk_is_reserved(self):
        plan = self.plan(state=PortfolioState(D("2000"), D("2000"), D("2000"), D("15")), lot_size=1)
        self.assertTrue(plan.approved)
        self.assertEqual(plan.quantity, 50)
        self.assertEqual(plan.risk, D("5.00"))
        blocked = self.plan(state=PortfolioState(D("2000"), D("2000"), D("2000"), D("20")))
        self.assertFalse(blocked.approved)

    def test_open_risk_reserves_daily_and_total_remaining_budget(self):
        plan = self.plan(state=PortfolioState(D("1965"), D("1965"), D("2000"), D("3")), lot_size=1)
        self.assertTrue(plan.approved)
        self.assertEqual(plan.risk, D("2.00"))

    def test_unknown_or_stale_marks_block_new_entries(self):
        plan = self.plan(state=PortfolioState(D("2000"), D("2000"), D("2000"), marks_valid=False))
        self.assertFalse(plan.approved)

    def test_net_reward_risk_gate_includes_costs(self):
        plan = self.plan(target=D("10.10"))
        self.assertFalse(plan.approved)
        self.assertTrue(any("1.5" in reason for reason in plan.reasons))
        at_boundary = self.plan(target=D("10.15"))
        self.assertTrue(at_boundary.approved)

    def test_nonpositive_net_reward_is_rejected(self):
        fees = FeeSchedule(D("0.0015"), D("0"), D("0"), D("0.07"))
        plan = self.plan(target=D("10.001"), fees=fees)
        self.assertFalse(plan.approved)
        self.assertLessEqual(plan.net_reward, 0)

    def test_nan_infinity_and_invalid_prices_cannot_generate_plan(self):
        for entry in ("NaN", "Infinity", "-Infinity", "0", "-1"):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.plan(entry=D(entry))
        for slip in ("NaN", "Infinity", "-0.1", "1"):
            with self.subTest(slip=slip), self.assertRaises(ValueError):
                self.plan(slippage_rate=D(slip))
        for lot in (0, -1, True, 1.5):
            with self.subTest(lot=lot), self.assertRaises(ValueError):
                self.plan(lot_size=lot)
        with self.assertRaises(ValueError):
            self.plan(symbol=" ")
        with self.assertRaises(ValueError):
            self.plan(stop=D("10"))

    def test_configuration_rejects_invalid_limits_and_fees(self):
        with self.assertRaises(ValueError):
            RiskLimits(initial_capital=D("NaN"))
        with self.assertRaises(ValueError):
            FeeSchedule(commission_rate=D("Infinity"))
        with self.assertRaises(ValueError):
            PortfolioState(D("-1"), D("2000"), D("2000"))


if __name__ == "__main__":
    unittest.main()

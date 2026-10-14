"""Local acceptance checks; NOT a Pine compiler or strategy backtest.

Checks the order-card contract independently using Decimal and audits the
source's HTF publication boundary. TradingView compilation is a separate gate.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from pathlib import Path
import re
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tradingview" / "intraday_assistant_v1.pine"


def order_card(entry, stop, cash="500", risk="2.50", buy="0.50", sell="0.50", tick="0.01"):
    """Decimal acceptance oracle, not the live indicator implementation."""
    e, s, cash, risk, buy, sell, tick = map(Decimal, (entry, stop, cash, risk, buy, sell, tick))
    if e <= 0 or s <= 0 or e <= s:
        return 0, None, None, None
    d = e - s
    affordable = (min(Decimal("450"), cash - Decimal("50")) - buy) / e
    risk_shares = (risk - buy - sell) / d
    quantity = max(0, int(min(affordable, risk_shares).to_integral_value(rounding=ROUND_FLOOR)))
    target = ((e + 3 * d) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    loss = quantity * d + buy + sell
    gain = quantity * (target - e) - buy - sell
    return quantity, target, loss, gain / loss if quantity and loss > 0 else None


def pine_tokens(text):
    # Strip strings/comments before checking delimiters. Preserve line numbers.
    return re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*', lambda m: " " * len(m.group()), text)


class AcceptanceChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_pine_v6_indicator_and_no_broker_orders(self):
        self.assertTrue(self.source.startswith("//@version=6\n"))
        self.assertIn("indicator(", self.source)
        self.assertNotRegex(pine_tokens(self.source), r"\bstrategy\.(?:entry|order|exit|close)\s*\(")

    def test_balanced_delimiters(self):
        stack = []
        pairs = {"(": ")", "[": "]", "{": "}"}
        for char in pine_tokens(self.source):
            if char in pairs:
                stack.append(pairs[char])
            elif char in pairs.values():
                self.assertTrue(stack, "Unexpected closing delimiter")
                self.assertEqual(stack.pop(), char)
        self.assertFalse(stack, "Unclosed delimiter")

    def test_htf_payload_fields_are_all_confirmed(self):
        payload = self.source.split("f_confirmed_payload() =>", 1)[1].split("\n[signal5", 1)[0]
        returned = re.findall(r"^    \[(.+)\]$", payload, re.M)[-1]
        fields = [field.strip() for field in returned.split(",")]
        self.assertEqual(len(fields), 18)
        self.assertTrue(all(re.fullmatch(r"[A-Za-z_]+\[1\]", field) for field in fields))
        self.assertIn('"5", f_confirmed_payload()', self.source)
        direction = self.source.split("f_direction_snapshot() =>", 1)[1].split("f_premarket() =>", 1)[0]
        for required in ("close[1]", "close[2]", "vw[1]", "vw[2]", "vw[3]"):
            self.assertIn(required, direction)

    def test_realtime_latches_and_late_arrival_gate(self):
        for name in ("handledStamp", "cardStamp", "cardState", "cardEntry", "cardExitSent"):
            self.assertRegex(self.source, rf"varip\s+\w+\s+{name}\s*=")
        self.assertIn("not lateArrival and not priceGone", self.source)
        self.assertIn("sameDirectionDay", self.source)
        self.assertIn("orCount == 6", self.source)
        self.assertIn("openWait == 3", self.source)
        self.assertIn("tailWait == 2", self.source)

    def test_required_manual_controls_and_safe_messages(self):
        for required in ("pauseNew", "blockDay", "premarketChecked", "blackoutSession", "chart.is_standard", "timeframe.multiplier == 1"):
            self.assertIn(required, self.source)
        self.assertIn("不是成交", self.source)
        self.assertIn("不是IBKR撤单结果", self.source)
        self.assertIn("须重建提醒", self.source)

    def test_documented_example_and_net_ratio(self):
        q, target, loss, ratio = order_card("40", "39.50")
        self.assertEqual(q, 3)
        self.assertEqual(target, Decimal("41.50"))
        self.assertEqual(loss, Decimal("2.50"))
        self.assertEqual(ratio, Decimal("1.40"))

    def test_fee_and_cash_rejections(self):
        self.assertEqual(order_card("40", "39.50", risk="0.70")[0], 0)
        self.assertEqual(order_card("40", "39.50", cash="75")[0], 0)
        self.assertEqual(order_card("40", "40")[0], 0)
        self.assertEqual(order_card("40", "41")[0], 0)
        self.assertEqual(order_card("500", "499")[0], 0)

    def test_tick_rounding_and_affordability(self):
        q, target, _, _ = order_card("44.99", "44.47", risk="50")
        self.assertEqual(q, 9)  # 10 shares + $0.50 fee would exceed $450.
        self.assertEqual(target, Decimal("46.55"))
        self.assertLessEqual(q * Decimal("44.99") + Decimal("0.50"), Decimal("450"))

    def test_grid_respects_cash_and_risk(self):
        for e in ("10", "40", "99.99", "224.50", "450"):
            for distance in ("0.02", "0.25", "0.50", "1.50", "3"):
                s = str(Decimal(e) - Decimal(distance))
                q, target, loss, _ = order_card(e, s)
                if q:
                    self.assertLessEqual(loss, Decimal("2.50"))
                    self.assertLessEqual(q * Decimal(e) + Decimal("0.50"), Decimal("450"))
                    self.assertGreaterEqual(target - Decimal(e), 3 * Decimal(distance))

    def test_dst_and_phase_cutoffs(self):
        eastern = ZoneInfo("America/New_York")
        summer = datetime(2026, 10, 14, 10, 0, tzinfo=eastern)
        winter = datetime(2026, 12, 14, 10, 0, tzinfo=eastern)
        self.assertEqual(summer.utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual(winter.utcoffset().total_seconds(), -5 * 3600)
        self.assertIn('TZ = "America/New_York"', self.source)
        self.assertIn("phase == 1 ? 645 : phase == 2 ? 810 : 915", self.source)
        self.assertIn("phase == 1 ? 675 : phase == 2 ? 840 : 945", self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)

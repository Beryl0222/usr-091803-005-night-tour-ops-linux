"""财务核算：门票/演出/商户引流三类分别核清，商户分账可追溯。"""

import unittest

from nighttour import TicketKind
from tests.helpers import T0, build_center, buy, set_now


class FinanceTest(unittest.TestCase):
    def setUp(self):
        set_now(T0)
        self.center = build_center()
        self.engine = self.center.engine
        self.finance = self.center.finance

    def test_three_revenue_kinds_are_separated(self):
        hold = self.engine.create_hold("s1", "g1", 3, "ctrip")
        self.engine.confirm_order(hold.id, "v1", [
            {"kind": "ticket", "quantity": 1, "unit_price": 8000},
            {"kind": "show", "quantity": 1, "unit_price": 18000},
            {"kind": "merchant_lead", "quantity": 1, "unit_price": 1000,
             "merchant_id": "m1"},
        ])
        summary = self.finance.summary()
        by_kind = summary["by_kind"]
        self.assertEqual(by_kind["ticket"]["income"], 8000)
        self.assertEqual(by_kind["show"]["income"], 18000)
        self.assertEqual(by_kind["merchant_lead"]["income"], 1000)
        self.assertEqual(summary["total"]["income"], 27000)

    def test_refund_netting_stays_within_kind(self):
        set_now(T0 - 4000)  # 早于退款截止窗，可全额退
        hold = self.engine.create_hold("s1", "g1", 2, "ctrip")
        self.engine.confirm_order(hold.id, "v1", [
            {"kind": "show", "quantity": 1, "unit_price": 18000},
            {"kind": "merchant_lead", "quantity": 1, "unit_price": 1000,
             "merchant_id": "m1"},
        ])
        self.engine.refund_order(self.engine.store.orders[hold.order_id].id)
        summary = self.finance.summary()
        self.assertEqual(summary["by_kind"]["show"]["net"], 0)
        self.assertEqual(summary["by_kind"]["merchant_lead"]["net"], 0)
        # 商户分账应结也归零
        settlements = self.finance.merchant_settlements()
        self.assertEqual(settlements[0]["payable"], 0)

    def test_merchant_payable_uses_locked_lead_rate(self):
        hold = self.engine.create_hold("s1", "g1", 2, "ctrip")
        self.engine.confirm_order(hold.id, "v1", [
            {"kind": "merchant_lead", "quantity": 2, "unit_price": 5000,
             "merchant_id": "m1", "lead_rate": 0.4},
        ])
        # 事后改默认费率不影响已售订单
        self.center.store.merchants["m1"].default_lead_rate = 0.9
        settlements = self.finance.merchant_settlements()
        self.assertEqual(settlements[0]["gross_income"], 10000)
        self.assertEqual(settlements[0]["payable"], 4000)

    def test_entries_can_be_filtered(self):
        buy(self.engine, "s1", "g1", 1, "v1", kind=TicketKind.TICKET, price=8000)
        show_entries = self.finance.entries(kind="show")
        ticket_entries = self.finance.entries(kind="ticket")
        self.assertEqual(len(show_entries), 0)
        self.assertEqual(len(ticket_entries), 1)


if __name__ == "__main__":
    unittest.main()

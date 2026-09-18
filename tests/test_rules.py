"""退款/改期/补偿：严格按购票时规则快照计算，规则事后修改不溯及既往。"""

import unittest

from nighttour import RuleError, RulePolicy, StateError, TicketKind
from tests.helpers import T0, build_center, buy, set_now

POLICY = RulePolicy(
    refund_cutoff_minutes=120,
    late_refund_ratio=0.5,
    after_start_refund_ratio=0.8,
    free_reschedule=False,
    reschedule_fee=500,
    compensation_amount=2000,
    compensation_on_migrate=True,
)


class RefundRuleTest(unittest.TestCase):
    def setUp(self):
        set_now(T0)
        self.center = build_center(policy=POLICY)
        self.engine = self.center.engine
        self.order = buy(self.engine, "s1", "g1", 2, "visitor-1", price=10000)

    def test_full_refund_long_before_start(self):
        set_now(T0 - 4000)  # 距开场 7600 秒 > 120 分钟截止窗
        plan = self.engine.evaluate_refund(self.order.id)
        self.assertEqual(plan["amount"], 20000)
        self.assertEqual(plan["disposition"], "refund")

    def test_half_refund_inside_cutoff_window(self):
        set_now(T0 + 1000)  # 距开场约 43 分钟，落在 120 分钟窗口内
        plan = self.engine.evaluate_refund(self.order.id)
        self.assertEqual(plan["amount"], 10000)
        self.assertEqual(plan["disposition"], "partial_refund")

    def test_no_refund_after_start_for_visitor(self):
        set_now(T0 + 3601)
        plan = self.engine.evaluate_refund(self.order.id)
        self.assertEqual(plan["amount"], 0)
        self.assertEqual(plan["disposition"], "unchanged")
        with self.assertRaises(RuleError):
            self.engine.refund_order(self.order.id)

    def test_operator_cancel_before_start_full_refund(self):
        result = self.engine.cancel_session("s1", "设备检修",
                                            idempotency_key="cancel-1")
        ids = [r["order_id"] for r in result["refunds"]]
        self.assertIn(self.order.id, ids)
        plan = next(r for r in result["refunds"]
                    if r["order_id"] == self.order.id)
        self.assertEqual(plan["amount"], 20000)

    def test_operator_cancel_after_start_uses_after_start_ratio(self):
        set_now(T0 + 3700)  # 已开场
        result = self.engine.cancel_session("s1", "舞台故障")
        plan = next(r for r in result["refunds"]
                    if r["order_id"] == self.order.id)
        self.assertEqual(plan["amount"], int(20000 * 0.8))

    def test_policy_change_after_purchase_does_not_affect_old_orders(self):
        # 事后把截止窗口改成 0 分钟（任何时刻游客都只能退 50%）
        session = self.engine._session("s1")
        session.policy = RulePolicy(
            refund_cutoff_minutes=0, late_refund_ratio=0.5,
            after_start_refund_ratio=0.0, free_reschedule=True,
            reschedule_fee=0, compensation_amount=0,
            compensation_on_migrate=False)
        set_now(T0 + 3600 - 100 * 60)  # 按老政策处于 120 分钟窗口内
        plan = self.engine.evaluate_refund(self.order.id)
        self.assertEqual(plan["amount"], 10000)  # 仍是老快照的 50%

    def test_reschedule_charges_fee_from_snapshot_policy(self):
        order2 = buy(self.engine, "s2", "g1", 1, "visitor-2")
        moved = self.engine.reschedule_order(
            self.order.id, "s2", "g1", idempotency_key="rs-1")
        self.assertEqual(moved.items[0].session_id, "s2")
        fees = [e for e in self.center.store.ledger
                if e.direction == "reschedule_fee"]
        self.assertEqual(sum(e.amount for e in fees), 500)
        # 老场次容量已释放
        view = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(view["session"]["occupied"], 0)

    def test_reschedule_into_full_session_fails_without_releasing_original(self):
        # 占满 s2（100 张）
        buy(self.engine, "s2", "g1", 60, "blocker-1")
        buy(self.engine, "s2", "g2", 40, "blocker-2")
        with self.assertRaises(Exception):
            self.engine.reschedule_order(self.order.id, "s2", "g1")
        view = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(view["session"]["occupied"], 2)

    def test_refund_idempotency_returns_first_result(self):
        first = self.engine.refund_order(
            self.order.id, idempotency_key="refund-1")
        second = self.engine.refund_order(
            self.order.id, idempotency_key="refund-1")
        self.assertEqual(first["amount"], second["amount"])
        refund_entries = [e for e in self.center.store.ledger
                          if e.direction == "refund"]
        self.assertEqual(len(refund_entries), 1)

    def test_refund_before_start_releases_capacity(self):
        self.engine.refund_order(self.order.id)
        view = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(view["session"]["occupied"], 0)


if __name__ == "__main__":
    unittest.main()

"""场次迁移与取消：容量搬运、装不下的订单按快照退款、补偿、幂等。"""

import unittest

from nighttour import SessionState, TicketKind
from tests.helpers import T0, build_center, buy, set_now


class MigrationTest(unittest.TestCase):
    def setUp(self):
        set_now(T0)
        self.center = build_center()
        self.engine = self.center.engine

    def test_migrate_moves_capacity_to_backup_and_marks_old_session(self):
        buy(self.engine, "s1", "g1", 20, "v1")
        result = self.engine.migrate_session(
            "s1", "a2", T0 + 7200, T0 + 9000,
            ["g3"], route_ids=["r2"], idempotency_key="mig-1")
        new_id = result["new_session_id"]
        self.assertEqual(self.engine._session("s1").state,
                         SessionState.MIGRATED)
        self.assertEqual(self.engine._session(new_id).migrated_from, "s1")
        self.assertEqual(result["capacity"]["session"]["occupied"], 20)
        # 老场次容量已清空
        old = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(old["session"]["occupied"], 0)

    def test_migrate_refunds_orders_that_do_not_fit_backup(self):
        buy(self.engine, "s1", "g1", 40, "fits-1")
        buy(self.engine, "s1", "g2", 30, "overflow")  # 备用区 60，装 40 后余 20
        result = self.engine.migrate_session(
            "s1", "a2", T0 + 7200, T0 + 9000, ["g3"], route_ids=["r2"])
        self.assertEqual(result["capacity"]["session"]["occupied"], 40)
        self.assertEqual(len(result["refunded"]), 1)
        refunded = result["refunded"][0]
        self.assertEqual(refunded["amount"], 300000)

    def test_migrate_grants_compensation_per_snapshot_policy(self):
        order = buy(self.engine, "s1", "g1", 2, "v1")
        self.engine.migrate_session(
            "s1", "a2", T0 + 7200, T0 + 9000, ["g3"], route_ids=["r2"])
        self.assertEqual(len(order.compensations), 1)
        self.assertEqual(order.compensations[0]["amount"], 2000)

    def test_migrate_is_idempotent_and_returns_same_new_session(self):
        buy(self.engine, "s1", "g1", 5, "v1")
        r1 = self.engine.migrate_session(
            "s1", "a2", T0 + 7200, T0 + 9000, ["g3"],
            idempotency_key="mig-x")
        r2 = self.engine.migrate_session(
            "s1", "a2", T0 + 7200, T0 + 9000, ["g3"],
            idempotency_key="mig-x")
        self.assertEqual(r1["new_session_id"], r2["new_session_id"])
        self.assertEqual(r2["capacity"]["session"]["occupied"], 5)

    def test_cancel_releases_unpaid_holds_and_refunds_paid_orders(self):
        hold = self.engine.create_hold("s1", "g2", 10, "ctrip")
        order = buy(self.engine, "s1", "g1", 5, "v1")
        result = self.engine.cancel_session("s1", "暴雨红色预警")
        self.assertIn(hold.id, result["released_holds"])
        self.assertEqual(result["refunds"][0]["order_id"], order.id)
        self.assertEqual(self.engine._session("s1").state,
                         SessionState.CANCELLED)

    def test_end_session_requires_clear_site_unless_forced(self):
        buy(self.engine, "s1", "g1", 5, "v1")
        from nighttour import StateError
        with self.assertRaises(StateError):
            self.engine.end_session("s1")
        result = self.engine.end_session("s1", force=True)
        self.assertEqual(result["remaining_occupied"], 5)
        self.assertEqual(self.engine._session("s1").state,
                         SessionState.CLOSED)


if __name__ == "__main__":
    unittest.main()

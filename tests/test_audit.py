"""审计还原：容量变化、游客调整、重新开放的依据都可由事件重建。"""

import unittest

from nighttour import AlertLevel, FacilityState
from tests.helpers import T0, build_center, buy, set_now


class AuditTest(unittest.TestCase):
    def setUp(self):
        set_now(T0)
        self.center = build_center()
        self.engine = self.center.engine
        self.audit = self.center.audit

    def test_timeline_replays_capacity_to_zero_after_checkout(self):
        order = buy(self.engine, "s1", "g1", 3, "v1")
        self.engine.checkout_order(order.id)
        timeline = self.audit.session_timeline("s1")
        nonzero = [r for r in timeline["capacity"]["final_replayed"]
                   if r["held"] or r["occupied"]]
        self.assertEqual(nonzero, [])
        # 检查点里应能看到锁位->确认->离场的变化
        events = [c["event"] for c in timeline["capacity"]["checkpoints"]]
        self.assertIn("order_confirmed", events)
        self.assertIn("order_checked_out", events)

    def test_timeline_lists_adjusted_visitors_and_refund_totals(self):
        self.engine.set_entrance_quota("g2", "s1", 1000)
        buy(self.engine, "s1", "g1", 5, "kept")
        overflow = buy(self.engine, "s1", "g2", 70, "overflow")
        result = self.engine.migrate_session(
            "s1", "a2", T0 + 7200, T0 + 9000, ["g3"], route_ids=["r2"])
        timeline = self.audit.session_timeline("s1")
        totals = timeline["visitor_adjustments"]["totals"]
        self.assertEqual(totals["affected_orders"], 2)
        self.assertEqual(totals["refund_total"], 700000)
        # 迁移成功订单有补偿（默认政策 2000/单）
        self.assertEqual(totals["compensation_total"], 2000)
        migrated_order = next(
            o for o in timeline["visitor_adjustments"]["orders"]
            if "migrate" in (o["dispositions"] or []))
        self.assertIsNotNone(migrated_order)
        self.assertEqual(result["new_session_id"],
                         timeline["related_session_ids"][0])

    def test_timeline_explains_why_reopen_was_allowed(self):
        self.engine.suspend_session("s1", "暴雨橙色预警")
        self.engine.set_facility_state("f1", FacilityState.FAULT, "灯具故障")
        self.engine.set_facility_state("f1", FacilityState.NORMAL, "复检完成")
        reason = "气象解除、灯光复检通过、现场清场确认"
        self.engine.resume_session(
            "s1", ["气象解除", "设备复检", "清场确认"], reason)
        timeline = self.audit.session_timeline("s1")
        reopen = timeline["reopen"]
        self.assertTrue(reopen["reopened"])
        self.assertEqual(reopen["why"], reason)
        checks = reopen["resumptions"][0]["verified_checks"]
        self.assertEqual(set(checks), {"气象解除", "设备复检", "清场确认"})
        self.assertIn("capacity_after", reopen["resumptions"][0])

    def test_timeline_covers_field_transfer_and_alert_recovery(self):
        order = buy(self.engine, "s1", "g1", 2, "v1", route="r1")
        alert = self.center.safety.raise_alert(
            AlertLevel.WARNING, "route", "r1", "路面湿滑",
            handler_id="sun",
            recovery_conditions=["排水完成", "路面复检"])
        self.center.safety.report_field_action(
            "restrict_route", {"route_id": "r1", "alert_id": alert.id},
            merge_key="m0")
        self.center.safety.report_field_action(
            "transfer_visitors",
            {"from_route_id": "r1", "to_route_id": "r2", "quantity": 2,
             "order_ids": [order.id], "session_id": "s1",
             "alert_id": alert.id},
            idempotency_key="t1", merge_key="m1")
        self.center.safety.recover_alert(
            alert.id, ["排水完成", "路面复检"], "排水复检通过")
        timeline = self.audit.session_timeline("s1")
        types_ = {e["type"] for e in timeline["events"]}
        self.assertIn("visitors_transferred", types_)
        self.assertIn("alert_recovered", types_)
        recovery = timeline["reopen"]["alert_recoveries"][0]
        self.assertEqual(recovery["unblocked"], ["r1"])

    def test_timeline_distinguishes_original_and_backup_capacity(self):
        buy(self.engine, "s1", "g1", 10, "v1")
        result = self.engine.migrate_session(
            "s1", "a2", T0 + 7200, T0 + 9000, ["g3"], route_ids=["r2"])
        timeline = self.audit.session_timeline("s1")
        final = {(r["scope"], r["target_id"], r["session_id"]): r["occupied"]
                 for r in timeline["capacity"]["final_replayed"]}
        # 老场次归零，新场次（迁移场）占 10
        self.assertEqual(final[("session", "s1", "s1")], 0)
        self.assertEqual(final[("session", result["new_session_id"],
                                result["new_session_id"])], 10)


if __name__ == "__main__":
    unittest.main()

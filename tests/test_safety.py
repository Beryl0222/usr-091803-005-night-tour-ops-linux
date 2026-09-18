"""安全告警与现场离线处置：风险看板、限流/转移/疏散、幂等与合并、恢复门禁。"""

import unittest

from nighttour import AlertLevel, CapacityError, StateError
from tests.helpers import T0, build_center, buy, set_now


class SafetyTest(unittest.TestCase):
    def setUp(self):
        set_now(T0)
        self.center = build_center()
        self.engine = self.center.engine
        self.safety = self.center.safety
        self.order = buy(self.engine, "s1", "g1", 4, "visitor-1", route="r1")

    def _alert(self):
        return self.safety.raise_alert(
            AlertLevel.CRITICAL, "route", "r1", "游线积水",
            handler_id="zhao",
            recovery_conditions=["排水完成", "路面复检"])

    def test_board_shows_risk_handler_and_conditions_per_route(self):
        alert = self._alert()
        board = self.safety.safety_board()
        row = next(r for r in board["routes"] if r["route_id"] == "r1")
        self.assertEqual(row["risk"], "critical")
        self.assertEqual(row["handler_id"], "zhao")
        self.assertEqual(row["alert_id"], alert.id)
        self.assertEqual(row["recovery_conditions"], ["排水完成", "路面复检"])

    def test_restrict_blocks_route_and_recovery_unblocks(self):
        self._alert()
        self.safety.report_field_action(
            "restrict_route",
            {"route_id": "r1", "reason": "积水限流"},
            merge_key="rr-1")
        self.assertTrue(self.engine._route("r1").blocked)
        # 限流后新锁位拒绝
        with self.assertRaises(StateError):
            self.engine.create_hold("s1", "g1", 1, "ctrip", route_id="r1")

    def test_recovery_requires_all_conditions(self):
        self._alert()
        self.safety.report_field_action(
            "restrict_route", {"route_id": "r1"}, merge_key="rr-1")
        with self.assertRaises(StateError):
            self.safety.recover_alert(self._open_alert_id(), ["排水完成"],
                                      "尝试解封")
        self.safety.recover_alert(
            self._open_alert_id(), ["排水完成", "路面复检"],
            "排水完成、复检无隐患")
        self.assertFalse(self.engine._route("r1").blocked)

    def _open_alert_id(self):
        return next(a.id for a in self.center.store.alerts.values())

    def test_repeated_field_report_with_same_idempotency_key_does_not_reexecute(self):
        self._alert()
        payload = {"from_route_id": "r1", "to_route_id": "r2",
                   "quantity": 2, "order_ids": [self.order.id],
                   "session_id": "s1"}
        first = self.safety.report_field_action(
            "transfer_visitors", payload,
            idempotency_key="offline-1", merge_key="tv-1")
        second = self.safety.report_field_action(
            "transfer_visitors", payload,
            idempotency_key="offline-1", merge_key="tv-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        # r2 上只应有 4 人（订单整单 4 人搬运一次），不翻倍
        reading = self.engine.store.reading(("route", "r2", "s1"))
        self.assertEqual(reading["occupied"], 4)

    def test_offline_batches_merge_under_same_merge_key(self):
        self._alert()
        # r1 上另有 3 名未记名散客
        self.engine.store._bucket(("route", "r1", "s1"))["occupied"] += 3
        base = {"from_route_id": "r1", "to_route_id": "r2",
                "session_id": "s1", "alert_id": self._open_alert_id()}
        # 记名 4 人（整单），随后再转 3 名散客
        r1 = self.safety.report_field_action(
            "transfer_visitors", {**base, "quantity": 4,
                                  "order_ids": [self.order.id]},
            idempotency_key="dev-a", merge_key="incident-1")
        r2 = self.safety.report_field_action(
            "transfer_visitors", {**base, "quantity": 3},
            idempotency_key="dev-b", merge_key="incident-1")
        self.assertEqual(r1["action"]["id"], r2["action"]["id"])
        # 合并后总数 7，处置记录只有一条
        self.assertEqual(r2["action"]["quantity"], 7)
        alert = next(iter(self.center.store.alerts.values()))
        transfers = [a for a in alert.actions
                     if a["type"] == "transfer_visitors"]
        self.assertEqual(len(transfers), 1)
        self.assertEqual(len(transfers[0]["reports"]), 2)
        # r2 在途 7 人：记名 4 + 散客 3
        reading = self.engine.store.reading(("route", "r2", "s1"))
        self.assertEqual(reading["occupied"], 7)

    def test_anonymous_evacuation_reduces_occupied_and_cannot_go_negative(self):
        self._alert()
        result = self.safety.report_field_action(
            "evacuate", {"session_id": "s1", "route_id": "r1", "quantity": 3},
            idempotency_key="ev-1", merge_key="ev-1")
        self.assertEqual(result["action"]["quantity"], 3)
        reading = self.engine.store.reading(("route", "r1", "s1"))
        self.assertEqual(reading["occupied"], 1)  # 4 - 3
        # 再疏散 5 人必须被拒绝（不能减成负数）
        with self.assertRaises(CapacityError):
            self.safety.report_field_action(
                "evacuate", {"session_id": "s1", "route_id": "r1",
                             "quantity": 5},
                idempotency_key="ev-2")

    def test_named_evacuation_releases_order_allocation(self):
        self._alert()
        self.safety.report_field_action(
            "evacuate", {"session_id": "s1", "route_id": "r1", "quantity": 4,
                         "order_ids": [self.order.id]},
            idempotency_key="ev-named", merge_key="ev-named")
        reading = self.engine.store.reading(("route", "r1", "s1"))
        self.assertEqual(reading["occupied"], 0)
        self.assertEqual(self.order.allocations, [])

    def test_assign_handler_is_recorded(self):
        alert = self._alert()
        self.safety.assign_handler(alert.id, "qian")
        self.assertEqual(self.center.store.alerts[alert.id].handler_id, "qian")


if __name__ == "__main__":
    unittest.main()

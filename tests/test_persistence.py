"""事件日志持久化：重启后从 JSONL 重建状态，离线重传依旧不重复执行。"""

import json
import os
import tempfile
import unittest

from nighttour import AlertLevel, OperationsCenter, TicketKind
from tests.helpers import T0, build_center, buy, set_now


class EventLogPersistenceTest(unittest.TestCase):
    def setUp(self):
        set_now(T0)
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "events.jsonl")

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)
        os.rmdir(self.tmp)

    def test_full_state_is_rebuilt_after_restart(self):
        center = build_center(event_log_path=self.path)
        engine = center.engine
        order = buy(engine, "s1", "g1", 3, "visitor-1")
        engine.create_hold("s1", "g2", 5, "ctrip", idempotency_key="hold-x")
        # 安全处置也留下记录
        alert = center.safety.raise_alert(
            AlertLevel.WARNING, "route", "r1", "湿滑",
            handler_id="zhao", recovery_conditions=["排水", "复检"])
        center.safety.report_field_action(
            "restrict_route", {"route_id": "r1", "alert_id": alert.id},
            idempotency_key="rr-x", merge_key="rr-x")

        # —— 重启：仅凭日志文件构建新中心 ——
        reopened = OperationsCenter(event_log_path=self.path,
                                    clock=lambda: NOW[0])

        # 资源/场次/订单/告警/台账全部复原
        self.assertIn("s1", reopened.store.sessions)
        self.assertIn(order.id, reopened.store.orders)
        self.assertEqual(reopened.store.orders[order.id].paid_amount, 30000)
        self.assertTrue(reopened.store.routes["r1"].blocked)
        self.assertEqual(reopened.store.alerts[alert.id].handler_id, "zhao")
        self.assertTrue(any(e.kind == TicketKind.SHOW
                            for e in reopened.store.ledger))

        # 容量计数折叠一致：s1 占 3、锁 5
        view = reopened.store.capacity_view(reopened.engine._session("s1"))
        self.assertEqual(view["session"]["occupied"], 3)
        self.assertEqual(view["session"]["held"], 5)

        # 幂等索引随日志恢复：同键回放首次结果，不重复占容、不重复限流
        hold = reopened.engine.create_hold(
            "s1", "g2", 5, "ctrip", idempotency_key="hold-x")
        self.assertEqual(hold.quantity, 5)
        view = reopened.store.capacity_view(reopened.engine._session("s1"))
        self.assertEqual(view["session"]["held"], 5)
        result = reopened.safety.report_field_action(
            "restrict_route", {"route_id": "r1", "alert_id": alert.id},
            idempotency_key="rr-x", merge_key="rr-x")
        self.assertTrue(result["replayed"])

        # 审计时间线在重启后仍可完整还原
        timeline = reopened.audit.session_timeline("s1")
        self.assertTrue(any(e["type"] == "order_confirmed"
                            for e in timeline["events"]))

    def test_log_file_is_jsonl_with_monotonic_seq(self):
        build_center(event_log_path=self.path)
        with open(self.path, encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(records)
        seqs = [r["seq"] for r in records]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))


if __name__ == "__main__":
    unittest.main()

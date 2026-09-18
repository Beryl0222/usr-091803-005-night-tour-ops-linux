"""领域规则测试：容量、快照结算、迁移、安全闭环、离线合并、分账与回放。"""

import threading
import unittest

from nightops import Operations
from nightops.errors import CapacityExceeded, Conflict, Validation
from nightops.models import LockState, ZoneStatus

DATE = "2026-09-18"
SLOT = "19:00-20:00"


def make_ops():
    ops = Operations()
    ops.create_zone("z-in-a", "入口A", "entrance", 10)
    ops.create_zone("z-in-b", "入口B", "entrance", 8)
    ops.create_zone("z-stage", "主舞台", "stage", 20)
    ops.create_zone("z-backup", "备用广场", "backup", 15)
    ops.create_zone("z-small", "小备用区", "backup", 1)
    ops.create_route("r-main", "夜游主线", ["z-in-a", "z-stage"])
    ops.upsert_policy("p-std", "标准退改", 0.8, True, 20.0)
    ops.create_event(
        "e-1", "光影秀首场", "show", DATE, SLOT,
        allocations=[{"zone_id": "z-in-a", "quota": 6}, {"zone_id": "z-in-b", "quota": 4}],
        backup_zone_id="z-backup",
    )
    return ops


def sell(ops, order_id, event_id, zone_id, visitors, lines=None, key=None):
    lock = ops.capacity.place_lock(key or f"key-{order_id}", event_id, zone_id, visitors)
    return ops.orders.create_order(
        order_id, event_id, f"v-{order_id}", [lock.lock_id], "p-std",
        lines or [{"kind": "ticket", "quantity": visitors, "unit_price": 100.0}],
    )


class CapacityTest(unittest.TestCase):
    def test_lock_respects_event_quota_and_zone_capacity(self):
        ops = make_ops()
        ops.capacity.place_lock("k1", "e-1", "z-in-a", 6)
        with self.assertRaises(CapacityExceeded):  # 入口配额已满
            ops.capacity.place_lock("k2", "e-1", "z-in-a", 1)
        ops.capacity.place_lock("k3", "e-1", "z-in-b", 4)
        with self.assertRaises(CapacityExceeded):  # 第二个入口配额也已满
            ops.capacity.place_lock("k4", "e-1", "z-in-b", 1)

    def test_zone_capacity_is_shared_across_events(self):
        ops = make_ops()
        ops.create_event("e-2", "加场", "show", DATE, SLOT,
                         allocations=[{"zone_id": "z-in-a", "quota": 6}], backup_zone_id="z-backup")
        ops.capacity.place_lock("k1", "e-1", "z-in-a", 6)
        ops.capacity.place_lock("k2", "e-2", "z-in-a", 4)
        with self.assertRaises(CapacityExceeded):  # 区域总容量 10，已占满
            ops.capacity.place_lock("k3", "e-2", "z-in-a", 1)

    def test_lock_is_idempotent_by_channel_key(self):
        ops = make_ops()
        first = ops.capacity.place_lock("same-key", "e-1", "z-in-a", 2)
        second = ops.capacity.place_lock("same-key", "e-1", "z-in-a", 2)
        self.assertEqual(first.lock_id, second.lock_id)
        view = ops.capacity.capacity_view("z-in-a", DATE, SLOT)
        self.assertEqual(view["held"], 2)  # 重试没有重复占座

    def test_concurrent_locks_never_exceed_quota(self):
        ops = make_ops()
        successes = []
        failures = []

        def worker(i):
            try:
                ops.capacity.place_lock(f"ch-{i}", "e-1", "z-in-a", 1)
                successes.append(i)
            except CapacityExceeded:
                failures.append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(successes), 6)  # 配额 6，一个不多
        self.assertEqual(len(failures), 14)
        view = ops.capacity.capacity_view("z-in-a", DATE, SLOT)
        self.assertEqual(view["held"], 6)

    def test_expired_lock_frees_capacity(self):
        clock = [1000.0]
        ops = Operations()
        ops.store.clock = lambda: clock[0]
        ops.create_zone("z", "入口", "entrance", 2)
        ops.create_event("e", "场", "tour", DATE, SLOT, allocations=[{"zone_id": "z", "quota": 2}])
        ops.capacity.place_lock("k1", "e", "z", 2, ttl_seconds=60)
        clock[0] += 120  # 锁位超时
        lock = ops.capacity.place_lock("k2", "e", "z", 2)  # 应能重新锁上
        self.assertEqual(lock.state, LockState.HELD)


class OrderSettlementTest(unittest.TestCase):
    def test_cancel_uses_purchase_time_policy_snapshot(self):
        ops = make_ops()
        sell(ops, "o-1", "e-1", "z-in-a", 2)  # 购票时退款比例 0.8
        ops.upsert_policy("p-std", "标准退改", 0.5, True, 20.0)  # 政策随后收紧
        result = ops.events.cancel_event("e-1", "暴雨红色预警", responsible="scenic")
        settlement = result["settlements"][0]
        self.assertEqual(settlement["refund"], 160.0)  # 200 * 0.8，而非 0.5
        self.assertEqual(settlement["compensation"], 40.0)  # 20 * 2 人

    def test_weather_cancel_pays_no_compensation(self):
        ops = make_ops()
        sell(ops, "o-1", "e-1", "z-in-a", 2)
        result = ops.events.cancel_event("e-1", "不可抗力", responsible="weather")
        self.assertEqual(result["settlements"][0]["compensation"], 0.0)

    def test_cancel_is_not_repeatable(self):
        ops = make_ops()
        sell(ops, "o-1", "e-1", "z-in-a", 1)
        ops.events.cancel_event("e-1", "预警")
        with self.assertRaises(Conflict):
            ops.events.cancel_event("e-1", "预警")

    def test_reschedule_moves_order_when_policy_allows(self):
        ops = make_ops()
        ops.create_event("e-2", "次日场", "show", "2026-09-19", SLOT,
                         allocations=[{"zone_id": "z-in-a", "quota": 6}], backup_zone_id="z-backup")
        order = sell(ops, "o-1", "e-1", "z-in-a", 2)
        result = ops.events.cancel_event("e-1", "暴雨", responsible="weather", reschedule_event_id="e-2")
        self.assertEqual(result["settlements"][0]["action"], "rescheduled")
        self.assertEqual(order.event_id, "e-2")
        self.assertEqual(order.status, "active")
        view = ops.capacity.capacity_view("z-in-a", "2026-09-19", SLOT)
        self.assertEqual(view["confirmed"], 2)
        # 改期不退款
        self.assertEqual(ops.finance.reconciliation()["accounts"]["ticket"]["refunds"], 0.0)

    def test_reschedule_falls_back_to_refund_when_target_full(self):
        ops = make_ops()
        ops.create_event("e-2", "次日场", "show", "2026-09-19", SLOT,
                         allocations=[{"zone_id": "z-in-a", "quota": 1}], backup_zone_id="z-backup")
        sell(ops, "o-1", "e-1", "z-in-a", 2)
        result = ops.events.cancel_event("e-1", "暴雨", responsible="weather", reschedule_event_id="e-2")
        self.assertEqual(result["settlements"][0]["action"], "refunded")


class RelocateTest(unittest.TestCase):
    def test_relocate_moves_visitors_to_backup_zone(self):
        ops = make_ops()
        sell(ops, "o-1", "e-1", "z-in-a", 2)
        sell(ops, "o-2", "e-1", "z-in-b", 3)
        result = ops.events.relocate_event("e-1", reason="暴雨预警，启用备用区域")
        self.assertEqual(result["visitors_moved"], 5)
        self.assertEqual(result["status"], "relocated")
        view = ops.capacity.capacity_view("z-backup", DATE, SLOT)
        self.assertEqual(view["confirmed"], 5)
        self.assertEqual(ops.capacity.capacity_view("z-in-a", DATE, SLOT)["confirmed"], 0)

    def test_relocate_rejects_insufficient_backup(self):
        ops = make_ops()
        sell(ops, "o-1", "e-1", "z-in-a", 2)
        with self.assertRaises(CapacityExceeded):
            ops.events.relocate_event("e-1", to_zone_id="z-small", reason="测试")

    def test_relocate_after_cancel(self):
        ops = make_ops()
        ops.events.cancel_event("e-1", "暴雨预警")
        result = ops.events.relocate_event("e-1", reason="改到备用区域重新开放")
        self.assertEqual(result["status"], "relocated")


class SafetyTest(unittest.TestCase):
    def test_alert_opens_route_risk_and_reopen_requires_closure(self):
        ops = make_ops()
        ops.safety.raise_alert("al-1", "weather", "high", ["z-stage"], "暴雨红色预警")
        view = ops.safety.route_risk_view("r-main")[0]
        self.assertEqual(view["level"], "high")
        self.assertEqual(view["status"], "open")
        ops.safety.set_risk_handler("r-main", "王安全", "预警解除且舞台复检合格")
        ops.safety.set_zone_status("z-stage", "closed")
        with self.assertRaises(Conflict):  # 告警未解除不能重开
            ops.safety.reopen_zone("z-stage", "李主任", "想提前开放")
        ops.safety.clear_alert("al-1")
        facility_alert = [a for a in ops.store.alerts.values() if a.kind == "facility"][0]
        ops.safety.clear_alert(facility_alert.alert_id)
        with self.assertRaises(Conflict):  # 风险未闭环仍不能重开
            ops.safety.reopen_zone("z-stage", "李主任", "想提前开放")
        ops.safety.resolve_risk("r-main")
        zone = ops.safety.reopen_zone("z-stage", "李主任", "预警解除，设备复检合格")
        self.assertEqual(zone.status, ZoneStatus.OPEN)

    def test_maintenance_auto_raises_facility_alert(self):
        ops = make_ops()
        ops.safety.set_zone_status("z-stage", "maintenance")
        alerts = [a for a in ops.store.alerts.values() if a.kind == "facility"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(ops.safety.route_risk_view("r-main")[0]["status"], "open")

    def test_zone_status_open_is_rejected_without_reopen(self):
        ops = make_ops()
        with self.assertRaises(Validation):
            ops.safety.set_zone_status("z-stage", "open")


class FieldMergeTest(unittest.TestCase):
    def test_offline_actions_merge_idempotently(self):
        ops = make_ops()
        sell(ops, "o-1", "e-1", "z-in-a", 2)
        action = {"action_id": "fa-1", "staff_id": "s-1", "type": "transfer",
                  "payload": {"order_ids": ["o-1"], "to_zone_id": "z-in-b"}}
        first = ops.field.apply_batch([action])[0]
        second = ops.field.apply_batch([action])[0]
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "duplicate")
        # 只执行了一次：z-in-b 恰好 2 人
        self.assertEqual(ops.capacity.capacity_view("z-in-b", DATE, SLOT)["confirmed"], 2)
        self.assertEqual(ops.capacity.capacity_view("z-in-a", DATE, SLOT)["confirmed"], 0)

    def test_flow_limit_override_and_overflow(self):
        ops = make_ops()
        sell(ops, "o-1", "e-1", "z-in-a", 4)
        result = ops.field.apply_batch([
            {"action_id": "fa-1", "staff_id": "s-1", "type": "flow_limit",
             "payload": {"zone_id": "z-in-a", "date": DATE, "slot": SLOT, "capacity": 3}}
        ])[0]
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["result"]["overflow"], 1)  # 已占 4，限到 3，超员 1
        with self.assertRaises(CapacityExceeded):  # 限流后无法再锁位
            ops.capacity.place_lock("k-x", "e-1", "z-in-a", 1)

    def test_failed_action_can_be_retried_with_same_id(self):
        ops = make_ops()
        bad = {"action_id": "fa-9", "staff_id": "s-1", "type": "transfer",
               "payload": {"order_ids": ["o-x"], "to_zone_id": "z-in-b"}}
        first = ops.field.apply_batch([bad])[0]
        self.assertEqual(first["status"], "failed")
        sell(ops, "o-1", "e-1", "z-in-a", 1)
        good = {"action_id": "fa-9", "staff_id": "s-1", "type": "transfer",
                "payload": {"order_ids": ["o-1"], "to_zone_id": "z-in-b"}}
        second = ops.field.apply_batch([good])[0]
        self.assertEqual(second["status"], "applied")  # 失败记录不占幂等键


class FinanceTest(unittest.TestCase):
    def test_reconciliation_separates_accounts(self):
        ops = make_ops()
        sell(ops, "o-1", "e-1", "z-in-a", 2, lines=[
            {"kind": "ticket", "quantity": 2, "unit_price": 120.0},
            {"kind": "show", "quantity": 2, "unit_price": 80.0},
            {"kind": "merchant", "quantity": 1, "unit_price": 30.0, "merchant_id": "m-tea"},
        ])
        ops.events.cancel_event("e-1", "暴雨", responsible="scenic")
        report = ops.finance.reconciliation()
        self.assertEqual(report["accounts"]["ticket"], {"sales": 240.0, "refunds": 192.0, "compensations": 0.0, "net": 48.0})
        self.assertEqual(report["accounts"]["show"], {"sales": 160.0, "refunds": 128.0, "compensations": 0.0, "net": 32.0})
        self.assertEqual(report["accounts"]["merchant"]["net"], 6.0)
        self.assertEqual(report["accounts"]["compensation"]["net"], -40.0)
        self.assertEqual(report["merchants"]["m-tea"]["net"], 6.0)

    def test_merchant_line_requires_merchant_id(self):
        ops = make_ops()
        lock = ops.capacity.place_lock("k1", "e-1", "z-in-a", 1)
        with self.assertRaises(Validation):
            ops.orders.create_order("o-1", "e-1", "v-1", [lock.lock_id], "p-std",
                                    [{"kind": "merchant", "quantity": 1, "unit_price": 30.0}])


class ReplayTest(unittest.TestCase):
    def test_replay_reconstructs_full_lifecycle(self):
        ops = make_ops()
        sell(ops, "o-1", "e-1", "z-in-a", 2)
        ops.safety.raise_alert("al-1", "weather", "high", ["z-in-a"], "暴雨预警")
        ops.safety.set_zone_status("z-in-a", "closed")
        ops.events.cancel_event("e-1", "暴雨红色预警", responsible="scenic")
        facility = [a for a in ops.store.alerts.values() if a.kind == "facility"][0]
        ops.safety.clear_alert("al-1")
        ops.safety.clear_alert(facility.alert_id)
        ops.safety.resolve_risk("r-main")
        ops.safety.reopen_zone("z-in-a", "李主任", "预警解除，疏散通道复检合格")

        replay = ops.events.replay_event("e-1")
        self.assertTrue(replay["capacity_changes"])      # 容量如何变化
        self.assertTrue(replay["visitor_adjustments"])   # 哪些游客被调整
        self.assertEqual(len(replay["reopen_records"]), 1)
        reopen = replay["reopen_records"][0]
        self.assertEqual(reopen["data"]["approver"], "李主任")
        self.assertIn("预警解除", reopen["data"]["reason"])  # 为何允许重新开放
        seqs = [entry["seq"] for entry in replay["timeline"]]
        self.assertEqual(seqs, sorted(seqs))


if __name__ == "__main__":
    unittest.main()

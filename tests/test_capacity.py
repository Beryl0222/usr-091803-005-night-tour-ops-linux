"""容量与并发：多入口配额、四维度上限、并发锁位绝不超额。"""

import threading
import unittest

from nighttour import CapacityError, StateError, TicketKind
from tests.helpers import T0, build_center, buy, set_now


class CapacityTest(unittest.TestCase):
    def setUp(self):
        set_now(T0)
        self.center = build_center()
        self.engine = self.center.engine

    def test_session_split_across_two_entrances(self):
        self.engine.create_hold("s1", "g1", 60, "ctrip")
        self.engine.create_hold("s1", "g2", 40, "meituan")
        view = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(view["session"]["used"], 100)
        self.assertEqual(view["entrances"]["g1"]["used"], 60)
        self.assertEqual(view["entrances"]["g2"]["used"], 40)

    def test_entrance_quota_cannot_be_exceeded(self):
        self.engine.create_hold("s1", "g1", 60, "ctrip")
        with self.assertRaises(CapacityError):
            self.engine.create_hold("s1", "g1", 1, "ctrip")

    def test_area_capacity_cannot_be_exceeded_even_with_free_entrance_quota(self):
        # g1 配额足够但区域只有 100
        self.engine.set_entrance_quota("g1", "s1", 1000)
        self.engine.create_hold("s1", "g1", 100, "ctrip")
        with self.assertRaises(CapacityError):
            self.engine.create_hold("s1", "g1", 1, "ctrip")

    def test_route_capacity_is_independent_dimension(self):
        # 游线 r1 上限 80（放开入口配额，让游线维度先生效）
        self.engine.set_entrance_quota("g1", "s1", 1000)
        for _ in range(8):
            self.engine.create_hold("s1", "g1", 10, "ctrip", route_id="r1")
        with self.assertRaises(CapacityError):
            self.engine.create_hold("s1", "g1", 1, "ctrip", route_id="r1")
        # 不走该游线的入场仍可
        self.engine.create_hold("s1", "g2", 1, "meituan")

    def test_concurrent_holds_never_break_area_capacity(self):
        self.engine.set_entrance_quota("g1", "s1", 1000)
        errors = []

        def hammer():
            for _ in range(100):
                try:
                    self.engine.create_hold("s1", "g1", 1, "ctrip")
                except CapacityError as error:
                    errors.append(str(error))

        threads = [threading.Thread(target=hammer) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        view = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(view["session"]["used"], 100)
        self.assertGreater(len(errors), 0)

    def test_concurrent_confirm_orders_never_break_capacity(self):
        # 50 个渠道同时锁 3 个名额并立即支付，区域 100 只能成 33 单
        self.engine.set_entrance_quota("g1", "s1", 1000)
        outcomes = {"ok": 0, "fail": 0}
        lock = threading.Lock()

        def purchase(index):
            try:
                hold = self.engine.create_hold(
                    "s1", "g1", 3, f"ch-{index}")
                self.engine.confirm_order(
                    hold.id, f"v-{index}",
                    [{"kind": "show", "quantity": 3, "unit_price": 10000}])
                with lock:
                    outcomes["ok"] += 1
            except CapacityError:
                with lock:
                    outcomes["fail"] += 1

        threads = [threading.Thread(target=purchase, args=(i,)) for i in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        view = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(view["session"]["occupied"], 99)
        self.assertEqual(outcomes["ok"], 33)
        self.assertEqual(outcomes["fail"], 17)

    def test_released_hold_returns_capacity(self):
        self.engine.set_entrance_quota("g1", "s1", 1000)
        hold = self.engine.create_hold("s1", "g1", 100, "ctrip")
        view = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(view["session"]["available"], 0)
        self.engine.release_hold(hold.id)
        view = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(view["session"]["available"], 100)

    def test_expired_holds_are_swept(self):
        self.engine.create_hold("s1", "g1", 50, "ctrip", ttl_seconds=60)
        set_now(T0 + 61)
        released = self.engine.sweep_expired_holds()
        self.assertEqual(len(released), 1)
        view = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(view["session"]["held"], 0)

    def test_idempotent_hold_returns_same_hold_without_double_counting(self):
        first = self.engine.create_hold("s1", "g1", 10, "ctrip",
                                        idempotency_key="hold-1")
        second = self.engine.create_hold("s1", "g1", 10, "ctrip",
                                         idempotency_key="hold-1")
        self.assertEqual(first.id, second.id)
        view = self.engine.store.capacity_view(self.engine._session("s1"))
        self.assertEqual(view["session"]["held"], 10)

    def test_suspended_session_rejects_new_holds(self):
        self.engine.suspend_session("s1", "暴雨预警")
        with self.assertRaises(StateError):
            self.engine.create_hold("s1", "g1", 1, "ctrip")

    def test_blocked_route_rejects_holds(self):
        self.engine._route("r1").blocked = True
        with self.assertRaises(StateError):
            self.engine.create_hold("s1", "g1", 1, "ctrip", route_id="r1")


if __name__ == "__main__":
    unittest.main()

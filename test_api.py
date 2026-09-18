"""HTTP 接口冒烟测试：路由、状态码与错误格式。"""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from nightops import Operations
from service import Handler

DATE = "2026-09-18"
SLOT = "19:00-20:00"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.ops = Operations()  # 独立实例，避免污染全局状态
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(f"{self.base}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            payload = json.load(error)
            error.close()
            return error.code, payload

    def test_full_booking_flow_over_http(self):
        status, _ = self.call("POST", "/zones", {"zone_id": "z-a", "name": "入口A", "kind": "entrance", "capacity": 5})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/zones", {"zone_id": "z-b", "name": "备用区", "kind": "backup", "capacity": 5})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/policies", {"policy_id": "p-1", "name": "标准", "refund_rate": 0.8,
                                                    "allow_reschedule": True, "compensation_per_ticket": 20})
        self.assertEqual(status, 201)
        status, event = self.call("POST", "/events", {"event_id": "e-1", "name": "光影秀", "kind": "show",
                                                      "date": DATE, "slot": SLOT,
                                                      "allocations": [{"zone_id": "z-a", "quota": 3}],
                                                      "backup_zone_id": "z-b"})
        self.assertEqual(status, 201)
        self.assertEqual(event["status"], "scheduled")

        status, lock = self.call("POST", "/locks", {"key": "ch-1", "event_id": "e-1", "zone_id": "z-a", "count": 2})
        self.assertEqual(status, 201)
        self.assertEqual(lock["state"], "held")

        status, order = self.call("POST", "/orders", {"order_id": "o-1", "event_id": "e-1", "visitor_id": "v-1",
                                                      "lock_ids": [lock["lock_id"]], "policy_id": "p-1",
                                                      "lines": [{"kind": "ticket", "quantity": 2, "unit_price": 100.0}]})
        self.assertEqual(status, 201)
        self.assertEqual(order["visitors"], 2)

        status, report = self.call("GET", "/finance/reconciliation")
        self.assertEqual(status, 200)
        self.assertEqual(report["accounts"]["ticket"]["sales"], 200.0)

        status, replay = self.call("GET", "/events/e-1/replay")
        self.assertEqual(status, 200)
        self.assertTrue(replay["capacity_changes"])

    def test_over_capacity_returns_409_with_detail(self):
        self.call("POST", "/zones", {"zone_id": "z-c", "name": "入口C", "kind": "entrance", "capacity": 1})
        self.call("POST", "/events", {"event_id": "e-2", "name": "小场", "kind": "tour",
                                      "date": DATE, "slot": SLOT,
                                      "allocations": [{"zone_id": "z-c", "quota": 1}]})
        self.call("POST", "/locks", {"key": "ch-a", "event_id": "e-2", "zone_id": "z-c", "count": 1})
        status, payload = self.call("POST", "/locks", {"key": "ch-b", "event_id": "e-2", "zone_id": "z-c", "count": 1})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "capacity_exceeded")

    def test_field_batch_and_unknown_route(self):
        self.call("POST", "/zones", {"zone_id": "z-d", "name": "入口D", "kind": "entrance", "capacity": 4})
        action = {"action_id": "fa-http-1", "staff_id": "s-1", "type": "flow_limit",
                  "payload": {"zone_id": "z-d", "date": DATE, "slot": SLOT, "capacity": 2}}
        status, first = self.call("POST", "/field-actions/batch", {"actions": [action]})
        self.assertEqual(status, 200)
        self.assertEqual(first["results"][0]["status"], "applied")
        status, second = self.call("POST", "/field-actions/batch", {"actions": [action]})
        self.assertEqual(second["results"][0]["status"], "duplicate")

        status, payload = self.call("GET", "/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_validation_error_returns_400(self):
        status, payload = self.call("POST", "/zones", {"zone_id": "z-x"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "validation")


if __name__ == "__main__":
    unittest.main()

"""HTTP API 端到端：通过真实 ThreadingHTTPServer 验证路由、错误码与幂等头。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from nighttour.api import make_handler
from tests.helpers import T0, build_center, set_now


class ApiTest(unittest.TestCase):
    def setUp(self):
        set_now(T0)
        self.center = build_center()
        handler = make_handler(self.center)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, body=None, headers=None, query=None):
        url = f"{self.base}{path}"
        if query:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(query)}"
        data = json.dumps(body or {}).encode("utf-8")
        request = Request(url, data=data if method == "POST" else None,
                          method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_hold_confirm_capacity_flow(self):
        status, hold = self.call("POST", "/holds", {
            "session_id": "s1", "entrance_id": "g1", "quantity": 3,
            "channel": "ctrip"})
        self.assertEqual(status, 200)
        status, order = self.call("POST", f"/holds/{hold['id']}/confirm", {
            "visitor_id": "v-http",
            "items": [{"kind": "show", "quantity": 3, "unit_price": 10000}]})
        self.assertEqual(status, 200)
        self.assertEqual(order["paid_amount"], 30000)
        status, view = self.call("GET", "/sessions/s1/capacity")
        self.assertEqual(status, 200)
        self.assertEqual(view["session"]["occupied"], 3)

    def test_over_capacity_returns_409(self):
        status, payload = self.call("POST", "/holds", {
            "session_id": "s1", "entrance_id": "g1", "quantity": 999})
        self.assertEqual(status, 409)
        self.assertIn("容量", payload["error"])

    def test_rule_violation_returns_422(self):
        # 暂停场次后再恢复，缺少恢复条件 -> 422
        self.call("POST", "/sessions/s1/suspend", {"reason": "暴雨"})
        status, payload = self.call("POST", "/sessions/s1/resume", {
            "recovery_checks": ["气象解除"], "reason": "尝试"})
        self.assertEqual(status, 422)
        self.assertIn("恢复条件", payload["error"])

    def test_not_found_returns_404(self):
        status, _ = self.call("GET", "/sessions/nope/capacity")
        self.assertEqual(status, 404)

    def test_idempotency_header_replays_first_result(self):
        headers = {"Idempotency-Key": "http-idem-1"}
        s1, h1 = self.call("POST", "/holds", {
            "session_id": "s1", "entrance_id": "g2", "quantity": 2},
            headers=headers)
        s2, h2 = self.call("POST", "/holds", {
            "session_id": "s1", "entrance_id": "g2", "quantity": 2},
            headers=headers)
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(h1["id"], h2["id"])

    def test_full_incident_flow_over_http(self):
        # 先在 r1 上产生 2 人占用
        _, hold = self.call("POST", "/holds", {
            "session_id": "s1", "entrance_id": "g1", "quantity": 2,
            "route_id": "r1"})
        self.call("POST", f"/holds/{hold['id']}/confirm", {
            "visitor_id": "v-incident",
            "items": [{"kind": "show", "quantity": 2, "unit_price": 10000}]})
        # 安全告警 -> 现场疏散（重传幂等）-> 财务 -> 审计
        status, alert = self.call("POST", "/alerts", {
            "level": "critical", "scope": "route", "target_id": "r1",
            "title": "积水", "handler_id": "zhao",
            "recovery_conditions": ["排水完成", "路面复检"]})
        self.assertEqual(status, 200)
        payload = {"action_type": "evacuate",
                   "payload": {"session_id": "s1", "route_id": "r1",
                               "quantity": 1},
                   "merge_key": "ev-http-1"}
        s1, r1 = self.call("POST", "/field/actions", payload,
                           headers={"Idempotency-Key": "ev-http-1"})
        s2, r2 = self.call("POST", "/field/actions", payload,
                           headers={"Idempotency-Key": "ev-http-1"})
        self.assertEqual((s1, s2), (200, 200))
        self.assertFalse(r1["replayed"])
        self.assertTrue(r2["replayed"])
        status, board = self.call("GET", "/safety/board")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["risk"] == "critical" for r in board["routes"]))
        status, summary = self.call("GET", "/finance/summary")
        self.assertEqual(status, 200)
        self.assertIn("ticket", summary["by_kind"])
        status, timeline = self.call("GET", "/sessions/s1/timeline")
        self.assertEqual(status, 200)
        self.assertIn("capacity", timeline)
        self.assertIn("reopen", timeline)


if __name__ == "__main__":
    unittest.main()

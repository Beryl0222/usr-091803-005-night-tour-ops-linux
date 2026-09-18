"""景区夜游资源调度的运行入口与 HTTP 接口。"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from nightops import Operations
from nightops.errors import DomainError, Validation

SERVICE_ID = "night-tour-ops"
SERVICE_NAME = "景区夜游资源调度"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


OPS = Operations()


# ---------- 参数解析 ----------

def _need(body, key):
    value = body.get(key)
    if value is None or value == "":
        raise Validation(f"缺少参数: {key}")
    return value


def _as_int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise Validation(f"参数 {name} 必须是整数")


def _as_float(value, name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise Validation(f"参数 {name} 必须是数字")


# ---------- 路由处理 ----------

def h_health(ops, params, body, query):
    return 200, health_payload()


def h_create_zone(ops, params, body, query):
    zone = ops.create_zone(
        zone_id=_need(body, "zone_id"),
        name=_need(body, "name"),
        kind=_need(body, "kind"),
        capacity=_as_int(_need(body, "capacity"), "capacity"),
    )
    return 201, zone.to_dict()


def h_list_zones(ops, params, body, query):
    return 200, {"zones": ops.list_zones()}


def h_zone_status(ops, params, body, query):
    zone = ops.safety.set_zone_status(params[0], _need(body, "status"))
    return 200, zone.to_dict()


def h_zone_reopen(ops, params, body, query):
    zone = ops.safety.reopen_zone(params[0], approver=body.get("approver", ""), reason=body.get("reason", ""))
    return 200, zone.to_dict()


def h_zone_capacity(ops, params, body, query):
    date = query.get("date", [None])[0]
    slot = query.get("slot", [None])[0]
    if not date or not slot:
        raise Validation("查询容量必须携带 date 与 slot")
    return 200, ops.capacity.capacity_view(params[0], date, slot)


def h_create_route(ops, params, body, query):
    route = ops.create_route(route_id=_need(body, "route_id"), name=_need(body, "name"), zone_ids=body.get("zone_ids") or [])
    return 201, route.to_dict()


def h_list_routes(ops, params, body, query):
    return 200, {"routes": ops.list_routes()}


def h_route_risk(ops, params, body, query):
    return 200, {"routes": ops.safety.route_risk_view(params[0])}


def h_route_risk_set(ops, params, body, query):
    risk = ops.safety.set_risk_handler(params[0], assignee=body.get("assignee", ""), recovery_conditions=body.get("recovery_conditions", ""))
    return 200, risk.to_dict()


def h_route_risk_resolve(ops, params, body, query):
    risk = ops.safety.resolve_risk(params[0])
    return 200, risk.to_dict()


def h_safety_routes(ops, params, body, query):
    return 200, {"routes": ops.safety.route_risk_view()}


def h_save_policy(ops, params, body, query):
    policy = ops.upsert_policy(
        policy_id=_need(body, "policy_id"),
        name=_need(body, "name"),
        refund_rate=_as_float(_need(body, "refund_rate"), "refund_rate"),
        allow_reschedule=bool(body.get("allow_reschedule", False)),
        compensation_per_ticket=_as_float(body.get("compensation_per_ticket", 0), "compensation_per_ticket"),
    )
    return 201, policy.to_dict()


def h_create_event(ops, params, body, query):
    event = ops.create_event(
        event_id=_need(body, "event_id"),
        name=_need(body, "name"),
        kind=_need(body, "kind"),
        date=_need(body, "date"),
        slot=_need(body, "slot"),
        allocations=body.get("allocations") or [],
        backup_zone_id=body.get("backup_zone_id"),
    )
    return 201, event.to_dict()


def h_list_events(ops, params, body, query):
    return 200, {"events": ops.list_events()}


def h_event_view(ops, params, body, query):
    return 200, ops.events.event_view(params[0])


def h_event_cancel(ops, params, body, query):
    result = ops.events.cancel_event(
        params[0],
        reason=body.get("reason", ""),
        responsible=body.get("responsible", "scenic"),
        reschedule_event_id=body.get("reschedule_event_id"),
    )
    return 200, result


def h_event_relocate(ops, params, body, query):
    result = ops.events.relocate_event(params[0], to_zone_id=body.get("to_zone_id"), reason=body.get("reason", ""))
    return 200, result


def h_event_finish(ops, params, body, query):
    event = ops.events.finish_event(params[0])
    return 200, event.to_dict()


def h_event_replay(ops, params, body, query):
    return 200, ops.events.replay_event(params[0])


def h_place_lock(ops, params, body, query):
    lock = ops.capacity.place_lock(
        key=_need(body, "key"),
        event_id=_need(body, "event_id"),
        zone_id=_need(body, "zone_id"),
        count=_as_int(_need(body, "count"), "count"),
        ttl_seconds=_as_int(body.get("ttl_seconds", 300), "ttl_seconds"),
    )
    return 201, lock.to_dict()


def h_release_lock(ops, params, body, query):
    lock = ops.capacity.release_lock(params[0])
    return 200, lock.to_dict()


def h_create_order(ops, params, body, query):
    order = ops.orders.create_order(
        order_id=_need(body, "order_id"),
        event_id=_need(body, "event_id"),
        visitor_id=_need(body, "visitor_id"),
        lock_ids=body.get("lock_ids") or [],
        policy_id=_need(body, "policy_id"),
        lines=body.get("lines") or [],
    )
    return 201, order.to_dict()


def h_get_order(ops, params, body, query):
    return 200, ops.orders.get_order(params[0]).to_dict()


def h_raise_alert(ops, params, body, query):
    alert = ops.safety.raise_alert(
        alert_id=_need(body, "alert_id"),
        kind=_need(body, "kind"),
        severity=_need(body, "severity"),
        zone_ids=body.get("zone_ids") or [],
        message=body.get("message", ""),
    )
    return 201, alert.to_dict()


def h_clear_alert(ops, params, body, query):
    alert = ops.safety.clear_alert(params[0])
    return 200, alert.to_dict()


def h_field_batch(ops, params, body, query):
    return 200, {"results": ops.field.apply_batch(body.get("actions") or [])}


def h_reconciliation(ops, params, body, query):
    event_id = query.get("event_id", [None])[0]
    return 200, ops.finance.reconciliation(event_id=event_id)


ROUTES = [
    ("GET", re.compile(r"^/health$"), h_health),
    ("POST", re.compile(r"^/zones$"), h_create_zone),
    ("GET", re.compile(r"^/zones$"), h_list_zones),
    ("POST", re.compile(r"^/zones/([\w-]+)/status$"), h_zone_status),
    ("POST", re.compile(r"^/zones/([\w-]+)/reopen$"), h_zone_reopen),
    ("GET", re.compile(r"^/zones/([\w-]+)/capacity$"), h_zone_capacity),
    ("POST", re.compile(r"^/routes$"), h_create_route),
    ("GET", re.compile(r"^/routes$"), h_list_routes),
    ("GET", re.compile(r"^/routes/([\w-]+)/risk$"), h_route_risk),
    ("POST", re.compile(r"^/routes/([\w-]+)/risk$"), h_route_risk_set),
    ("POST", re.compile(r"^/routes/([\w-]+)/risk/resolve$"), h_route_risk_resolve),
    ("GET", re.compile(r"^/safety/routes$"), h_safety_routes),
    ("POST", re.compile(r"^/policies$"), h_save_policy),
    ("POST", re.compile(r"^/events$"), h_create_event),
    ("GET", re.compile(r"^/events$"), h_list_events),
    ("GET", re.compile(r"^/events/([\w-]+)$"), h_event_view),
    ("POST", re.compile(r"^/events/([\w-]+)/cancel$"), h_event_cancel),
    ("POST", re.compile(r"^/events/([\w-]+)/relocate$"), h_event_relocate),
    ("POST", re.compile(r"^/events/([\w-]+)/finish$"), h_event_finish),
    ("GET", re.compile(r"^/events/([\w-]+)/replay$"), h_event_replay),
    ("POST", re.compile(r"^/locks$"), h_place_lock),
    ("POST", re.compile(r"^/locks/([\w-]+)/release$"), h_release_lock),
    ("POST", re.compile(r"^/orders$"), h_create_order),
    ("GET", re.compile(r"^/orders/([\w-]+)$"), h_get_order),
    ("POST", re.compile(r"^/alerts$"), h_raise_alert),
    ("POST", re.compile(r"^/alerts/([\w-]+)/clear$"), h_clear_alert),
    ("POST", re.compile(r"^/field-actions/batch$"), h_field_batch),
    ("GET", re.compile(r"^/finance/reconciliation$"), h_reconciliation),
]


class Handler(BaseHTTPRequestHandler):
    """JSON 路由分发；业务状态挂在 server.ops 上，便于测试隔离。"""

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _ops(self):
        return getattr(self.server, "ops", None) or OPS

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise Validation("请求体不是合法的 JSON")
        if not isinstance(data, dict):
            raise Validation("请求体必须是 JSON 对象")
        return data

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(parsed.path)
            if not match:
                continue
            try:
                body = self._read_json() if method == "POST" else {}
                status, payload = handler(self._ops(), match.groups(), body, query)
            except DomainError as exc:
                status = exc.status
                payload = {"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}}
            except Exception as exc:  # 兜底，避免连接被静默断开
                status = 500
                payload = {"error": {"code": "internal", "message": str(exc)}}
            self._send(status, payload)
            return
        self._send(404, {"error": {"code": "not_found", "message": "接口不存在"}})

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        smoke = Operations()
        smoke.create_zone("z-check", "自检入口", "entrance", 1)
        assert smoke.list_zones()[0]["zone_id"] == "z-check"
        print("基础检查通过")
        return
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.ops = OPS
    server.serve_forever()


if __name__ == "__main__":
    main()

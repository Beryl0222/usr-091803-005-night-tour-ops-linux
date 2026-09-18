"""HTTP API：把运营中心各服务暴露为 JSON 接口。

仅依赖标准库；``/health`` 的既有契约由 service.py 保留。
所有写接口都接受请求头 ``Idempotency-Key``（也可在请求体中给
``idempotency_key``），重传返回首次结果且不重复执行。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .models import (
    AlertLevel,
    FacilityState,
    RulePolicy,
    TicketKind,
)
from .store import CapacityError, NotFoundError, StateError
from .engine import RuleError


def to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ApiRouter:
    """极简路由：(method, pattern) -> handler(center, body, query, params)。"""

    def __init__(self, center):
        self.center = center
        self.routes: list[tuple[str, str, Callable]] = []
        self._register()

    def route(self, method: str, pattern: str):
        def decorator(func):
            self.routes.append((method, pattern.strip("/"), func))
            return func
        return decorator

    def dispatch(self, method: str, path: str, body: dict, query: dict,
                 idempotency_key: str | None, actor: str | None):
        parts = [p for p in path.strip("/").split("/") if p]
        for http_method, pattern, func in self.routes:
            if http_method != method:
                continue
            pattern_parts = [p for p in pattern.split("/") if p]
            if len(pattern_parts) != len(parts):
                continue
            params = {}
            for token, value in zip(pattern_parts, parts):
                if token.startswith("{") and token.endswith("}"):
                    params[token[1:-1]] = value
                elif token != value:
                    break
            else:
                kwargs = dict(params)
                return func(body, query, kwargs, idempotency_key,
                            actor or "api")
        raise ApiError(404, f"未找到接口: {method} {path}")

    # ------------------------------------------------------------------
    # 路由注册
    # ------------------------------------------------------------------

    def _register(self) -> None:  # noqa: C901 - 路由表集中可读
        engine = self.center.engine
        safety = self.center.safety
        finance = self.center.finance
        audit = self.center.audit
        r = self.route

        def policy_from(body: dict) -> RulePolicy | None:
            data = body.get("policy")
            return RulePolicy.from_dict(data) if data else None

        # -- 资源管理 ----------------------------------------------------
        @r("POST", "admin/areas")
        def create_area(body, query, params, idem, actor):
            return engine.add_area(
                body["id"], body["name"], int(body["capacity"]),
                kind=body.get("kind", "general"),
                backup_area_id=body.get("backup_area_id"))

        @r("POST", "admin/entrances")
        def create_entrance(body, query, params, idem, actor):
            return engine.add_entrance(
                body["id"], body["name"], body["area_id"],
                quotas=body.get("quotas"))

        @r("POST", "admin/entrances/{entrance_id}/quotas")
        def set_quota(body, query, params, idem, actor):
            engine.set_entrance_quota(params["entrance_id"],
                                      body["session_id"], int(body["quota"]))
            return {"ok": True}

        @r("POST", "admin/routes")
        def create_route(body, query, params, idem, actor):
            return engine.add_route(
                body["id"], body["name"], int(body["capacity"]),
                area_ids=body.get("area_ids"))

        @r("POST", "admin/facilities")
        def create_facility(body, query, params, idem, actor):
            return engine.add_facility(
                body["id"], body["name"], body["area_id"])

        @r("POST", "admin/facilities/{facility_id}/state")
        def facility_state(body, query, params, idem, actor):
            return engine.set_facility_state(
                params["facility_id"], FacilityState(body["state"]),
                detail=body.get("detail", ""), actor=actor,
                idempotency_key=idem)

        @r("POST", "admin/merchants")
        def create_merchant(body, query, params, idem, actor):
            return engine.add_merchant(
                body["id"], body["name"],
                default_lead_rate=float(body.get("default_lead_rate", 0.0)))

        @r("POST", "admin/sessions")
        def create_session(body, query, params, idem, actor):
            return engine.create_session(
                body["id"], body["title"], body["area_id"],
                TicketKind(body.get("kind", "ticket")),
                float(body["start_at"]), float(body["end_at"]),
                entrance_ids=body.get("entrance_ids"),
                route_ids=body.get("route_ids"),
                policy=policy_from(body),
                capacity_override=body.get("capacity_override"),
                reopen_conditions=body.get("reopen_conditions"))

        @r("GET", "sessions/{session_id}/capacity")
        def capacity(body, query, params, idem, actor):
            return engine.store.capacity_view(engine._session(params["session_id"]))

        # -- 售票 --------------------------------------------------------
        @r("POST", "holds")
        def create_hold(body, query, params, idem, actor):
            return engine.create_hold(
                body["session_id"], body["entrance_id"], int(body["quantity"]),
                channel=body.get("channel", actor),
                ttl_seconds=int(body.get("ttl_seconds", 120)),
                route_id=body.get("route_id"),
                idempotency_key=idem)

        @r("POST", "holds/{hold_id}/release")
        def release_hold(body, query, params, idem, actor):
            return engine.release_hold(
                params["hold_id"], reason=body.get("reason", "cancelled"),
                actor=actor, idempotency_key=idem)

        @r("POST", "holds/{hold_id}/confirm")
        def confirm(body, query, params, idem, actor):
            return engine.confirm_order(
                params["hold_id"], body["visitor_id"], body["items"],
                channel=body.get("channel", actor),
                idempotency_key=idem)

        @r("GET", "orders/{order_id}/refund/evaluate")
        def eval_refund(body, query, params, idem, actor):
            at = float(query["at"][0]) if query.get("at") else None
            return engine.evaluate_refund(
                params["order_id"], at=at,
                operator_reason=query.get("reason", ["visitor_request"])[0])

        @r("POST", "orders/{order_id}/refund")
        def refund(body, query, params, idem, actor):
            return engine.refund_order(
                params["order_id"], reason=body.get("reason", "visitor_request"),
                actor=actor, ratio_override=body.get("ratio_override"),
                idempotency_key=idem)

        @r("POST", "orders/{order_id}/reschedule")
        def reschedule(body, query, params, idem, actor):
            return engine.reschedule_order(
                params["order_id"], body["to_session_id"], body["entrance_id"],
                route_id=body.get("route_id"), actor=actor,
                idempotency_key=idem)

        @r("POST", "orders/{order_id}/compensation")
        def compensate(body, query, params, idem, actor):
            return engine.grant_compensation(
                params["order_id"], int(body["amount"]),
                body.get("memo", ""), actor=actor, idempotency_key=idem)

        @r("POST", "orders/{order_id}/checkout")
        def checkout(body, query, params, idem, actor):
            return engine.checkout_order(
                params["order_id"], actor=actor, idempotency_key=idem)

        # -- 场次调度 ----------------------------------------------------
        @r("POST", "sessions/{session_id}/suspend")
        def suspend(body, query, params, idem, actor):
            return engine.suspend_session(
                params["session_id"], body["reason"], actor=actor,
                idempotency_key=idem)

        @r("POST", "sessions/{session_id}/resume")
        def resume(body, query, params, idem, actor):
            return engine.resume_session(
                params["session_id"], body["recovery_checks"],
                body["reason"], actor=actor, idempotency_key=idem)

        @r("POST", "sessions/{session_id}/cancel")
        def cancel(body, query, params, idem, actor):
            return engine.cancel_session(
                params["session_id"], body["reason"], actor=actor,
                auto_refund=bool(body.get("auto_refund", True)),
                idempotency_key=idem)

        @r("POST", "sessions/{session_id}/migrate")
        def migrate(body, query, params, idem, actor):
            return engine.migrate_session(
                params["session_id"], body["backup_area_id"],
                float(body["new_start_at"]), float(body["new_end_at"]),
                body["entrance_ids"], route_ids=body.get("route_ids"),
                title=body.get("title"), actor=actor,
                idempotency_key=idem)

        @r("POST", "sessions/{session_id}/end")
        def end(body, query, params, idem, actor):
            return engine.end_session(
                params["session_id"], actor=actor,
                force=bool(body.get("force", False)))

        # -- 安全 --------------------------------------------------------
        @r("POST", "alerts")
        def raise_alert(body, query, params, idem, actor):
            return safety.raise_alert(
                AlertLevel(body.get("level", "warning")),
                body["scope"], body["target_id"], body["title"],
                detail=body.get("detail", ""),
                handler_id=body.get("handler_id", ""),
                recovery_conditions=body.get("recovery_conditions"),
                actor=actor, idempotency_key=idem)

        @r("POST", "alerts/{alert_id}/assign")
        def assign(body, query, params, idem, actor):
            return safety.assign_handler(
                params["alert_id"], body["handler_id"], actor=actor)

        @r("POST", "alerts/{alert_id}/recover")
        def recover(body, query, params, idem, actor):
            return safety.recover_alert(
                params["alert_id"], body["recovery_checks"], body["reason"],
                actor=actor, idempotency_key=idem)

        @r("POST", "alerts/{alert_id}/close")
        def close_alert(body, query, params, idem, actor):
            return safety.close_alert(
                params["alert_id"], note=body.get("note", ""), actor=actor)

        @r("GET", "safety/board")
        def board(body, query, params, idem, actor):
            return safety.safety_board()

        @r("POST", "field/actions")
        def field_action(body, query, params, idem, actor):
            return safety.report_field_action(
                body["action_type"], body.get("payload", {}),
                actor=actor, idempotency_key=idem,
                merge_key=body.get("merge_key"))

        # -- 财务 --------------------------------------------------------
        @r("GET", "finance/summary")
        def summary(body, query, params, idem, actor):
            return finance.summary(session_id=query.get("session_id", [None])[0])

        @r("GET", "finance/merchants")
        def merchants(body, query, params, idem, actor):
            return finance.merchant_settlements(
                session_id=query.get("session_id", [None])[0])

        @r("GET", "finance/entries")
        def entries(body, query, params, idem, actor):
            return finance.entries(
                kind=query.get("kind", [None])[0],
                merchant_id=query.get("merchant_id", [None])[0],
                session_id=query.get("session_id", [None])[0])

        # -- 审计 --------------------------------------------------------
        @r("GET", "sessions/{session_id}/timeline")
        def timeline(body, query, params, idem, actor):
            return audit.session_timeline(params["session_id"])


def make_handler(center) -> type[BaseHTTPRequestHandler]:
    router = ApiRouter(center)

    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method: str):
            parsed = urlparse(self.path)
            if method == "GET":
                body: dict[str, Any] = {}
            else:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._write_json(400, {"error": "请求体不是合法 JSON"})
                    return
            idem = (self.headers.get("Idempotency-Key")
                    or body.get("idempotency_key"))
            actor = self.headers.get("X-Actor")
            query = parse_qs(parsed.query)
            try:
                result = router.dispatch(
                    method, parsed.path, body, query, idem, actor)
            except ApiError as error:
                self._write_json(error.status, {"error": error.message})
                return
            except NotFoundError as error:
                self._write_json(404, {"error": str(error)})
                return
            except (CapacityError, StateError) as error:
                self._write_json(409, {"error": str(error)})
                return
            except RuleError as error:
                self._write_json(422, {"error": str(error)})
                return
            except (KeyError, TypeError, ValueError) as error:
                self._write_json(400, {"error": f"参数错误: {error}"})
                return
            except Exception as error:  # noqa: BLE001 - 兜底，避免连接挂死
                self._write_json(500, {"error": f"内部错误: {error}"})
                return
            self._write_json(200, to_jsonable(result))

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _write_json(self, status: int, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            return

    return Handler

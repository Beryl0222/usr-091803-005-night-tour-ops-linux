"""安全与现场处置。

负责三件事：
1. 每条游线/区域/设施的风险告警都有级别、处置人、恢复条件与处置记录；
2. 限流、转移、疏散等现场动作可离线分批上报，按幂等键去重、按 merge_key
   合并计数，重传绝不重复执行；
3. 恢复时逐条核验恢复条件，未全部满足不得解除限流/重开，"为什么允许
   重新开放" 连同核验证据写入事件。
"""

from __future__ import annotations

from typing import Any, Optional

from .models import Alert, AlertLevel, AlertState
from .store import CapacityError, NotFoundError, StateError


class SafetyService:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    # ------------------------------------------------------------------
    # 告警
    # ------------------------------------------------------------------

    def raise_alert(self, level: AlertLevel, scope: str, target_id: str,
                    title: str, detail: str = "", handler_id: str = "",
                    recovery_conditions: Optional[list[str]] = None,
                    actor: str = "safety",
                    idempotency_key: Optional[str] = None) -> Alert:
        with self.store.lock:
            if self.store.replayed(idempotency_key) is not None:
                return self._alert_by_target(scope, target_id)
            self._validate_target(scope, target_id)
            level = level if isinstance(level, AlertLevel) else AlertLevel(level)
            alert = Alert(
                id=self.store.next_id("alert"),
                level=level, scope=scope, target_id=target_id,
                title=title, detail=detail, created_at=self.store.now(),
                handler_id=handler_id,
                recovery_conditions=list(recovery_conditions or []),
            )
            self.store.alerts[alert.id] = alert
            self.store.record("alert_raised", {
                "alert_id": alert.id, "level": level.value, "scope": scope,
                "target_id": target_id, "title": title, "detail": detail,
                "handler_id": handler_id,
                "recovery_conditions": alert.recovery_conditions,
            }, actor=actor, idempotency_key=idempotency_key)
            return alert

    def assign_handler(self, alert_id: str, handler_id: str,
                       actor: str = "safety") -> Alert:
        with self.store.lock:
            alert = self._alert(alert_id)
            alert.handler_id = handler_id
            self.store.record("alert_assigned", {
                "alert_id": alert_id, "handler_id": handler_id,
            }, actor=actor)
            return alert

    def update_conditions(self, alert_id: str,
                          recovery_conditions: list[str]) -> Alert:
        with self.store.lock:
            alert = self._alert(alert_id)
            alert.recovery_conditions = list(recovery_conditions)
            self.store.record("alert_conditions_updated", {
                "alert_id": alert_id,
                "recovery_conditions": recovery_conditions,
            }, actor="safety")
            return alert

    # ------------------------------------------------------------------
    # 现场动作（限流 / 转移 / 疏散），均幂等、可合并
    # ------------------------------------------------------------------

    def report_field_action(self, action_type: str, payload: dict[str, Any],
                            actor: str = "field",
                            idempotency_key: Optional[str] = None,
                            merge_key: Optional[str] = None,
                            at: Optional[float] = None) -> dict[str, Any]:
        """上报一条现场动作。

        - idempotency_key 相同：视为设备重传，回放首次结果，不再执行；
        - merge_key 相同且动作/目标相同：合并到同一条处置记录并累加人数，
          副作用只执行一次；
        - 否则新建处置记录并执行相应副作用。
        """
        with self.store.lock:
            replayed = self.store.replayed(idempotency_key)
            if replayed is not None:
                return {"replayed": True, "action": replayed.data["action"]}
            now = at if at is not None else self.store.now()
            if action_type == "restrict_route":
                result = self._do_restrict_route(payload, actor, now, merge_key)
            elif action_type == "transfer_visitors":
                result = self._do_transfer(payload, actor, now, merge_key)
            elif action_type == "evacuate":
                result = self._do_evacuate(payload, actor, now, merge_key)
            else:
                raise StateError(f"未知现场动作: {action_type}")
            self.store.record("field_action_reported", {
                "action": result, "action_type": action_type,
                "payload": payload, "merge_key": merge_key,
            }, actor=actor, idempotency_key=idempotency_key, at=now)
            return {"replayed": False, "action": result}

    def _do_restrict_route(self, payload: dict, actor: str, now: float,
                           merge_key: Optional[str]) -> dict[str, Any]:
        route_id = payload["route_id"]
        reason = payload.get("reason", "现场限流")
        alert_id = payload.get("alert_id")
        route = self.engine._route(route_id)
        merged = False
        existing = self._find_action(alert_id, merge_key, "restrict_route",
                                     route_id)
        if existing is not None:
            existing["reports"].append({"by": actor, "at": now})
            merged = True
            action = existing
        else:
            route.blocked = True
            route.blocked_reason = reason
            action = {
                "id": self.store.next_id("act"),
                "type": "restrict_route", "route_id": route_id,
                "reason": reason, "alert_id": alert_id,
                "merge_key": merge_key,
                "at": now, "by": actor, "reports": [{"by": actor, "at": now}],
            }
            self._attach_action(alert_id, action)
        self.store.record("route_restricted", {
            "route_id": route_id, "reason": reason, "alert_id": alert_id,
            "merged": merged,
        }, actor=actor, at=now)
        return action

    def _do_transfer(self, payload: dict, actor: str, now: float,
                     merge_key: Optional[str]) -> dict[str, Any]:
        from_route_id = payload["from_route_id"]
        to_route_id = payload.get("to_route_id")
        quantity = int(payload.get("quantity", 0))
        order_ids = payload.get("order_ids") or []
        alert_id = payload.get("alert_id")
        self.engine._route(from_route_id)  # 校验存在
        if quantity < 0:
            raise StateError("转移人数不能为负")

        existing = self._find_action(alert_id, merge_key, "transfer_visitors",
                                     from_route_id, to_route_id)
        effects: list[dict[str, Any]] = []
        if existing is not None:
            existing["quantity"] += quantity
            existing["reports"].append({"by": actor, "at": now,
                                        "quantity": quantity})
            existing["order_ids"] = sorted(set(existing["order_ids"] + order_ids))
            merged = True
            action = existing
        else:
            merged = False
            action = {
                "id": self.store.next_id("act"),
                "type": "transfer_visitors",
                "from_route_id": from_route_id, "to_route_id": to_route_id,
                "quantity": quantity, "order_ids": list(order_ids),
                "alert_id": alert_id, "merge_key": merge_key,
                "at": now, "by": actor,
                "reports": [{"by": actor, "at": now, "quantity": quantity}],
            }
            self._attach_action(alert_id, action)

        # 记名游客：把订单占用从老游线搬到目标游线（目标容量逐单校验）
        processed = set(existing.get("processed_order_ids", [])) if existing else set()
        named_now = 0
        for order_id in order_ids:
            if order_id in processed:
                # 合并重传携带的同一订单不再重复搬运
                continue
            order = self.engine._order(order_id)
            before = self._route_occupied(order, from_route_id)
            effects += self._transfer_order_allocations(
                order, from_route_id, to_route_id
            )
            after = self._route_occupied(order, from_route_id)
            named_now += max(0, before - after)
            processed.add(order_id)
        # 未记名散客：报告总人数扣除本批记名实际移动人数
        anonymous = max(0, quantity - named_now)
        if anonymous > 0 and to_route_id:
            effects += self._move_anonymous(from_route_id, to_route_id,
                                            payload.get("session_id"), anonymous)
        action["processed_order_ids"] = sorted(processed)
        action["capacity_effects"] = action.get("capacity_effects", []) + effects
        self.store.record("visitors_transferred", {
            "from_route_id": from_route_id, "to_route_id": to_route_id,
            "quantity": quantity, "order_ids": order_ids,
            "session_id": payload.get("session_id"),
            "alert_id": alert_id, "merged": merged,
            "capacity_effects": effects,
        }, actor=actor, at=now)
        return action

    def _do_evacuate(self, payload: dict, actor: str, now: float,
                     merge_key: Optional[str]) -> dict[str, Any]:
        session_id = payload.get("session_id")
        route_id = payload.get("route_id")
        quantity = int(payload.get("quantity", 0))
        order_ids = payload.get("order_ids") or []
        alert_id = payload.get("alert_id")
        effects: list[dict[str, Any]] = []

        existing = self._find_action(alert_id, merge_key, "evacuate",
                                     route_id or session_id)
        if existing is not None:
            existing["quantity"] += quantity
            existing["reports"].append({"by": actor, "at": now,
                                        "quantity": quantity})
            existing["order_ids"] = sorted(set(existing["order_ids"] + order_ids))
            merged = True
            action = existing
        else:
            merged = False
            action = {
                "id": self.store.next_id("act"),
                "type": "evacuate", "session_id": session_id,
                "route_id": route_id, "quantity": quantity,
                "order_ids": list(order_ids), "alert_id": alert_id,
                "at": now, "by": actor,
                "reports": [{"by": actor, "at": now, "quantity": quantity}],
            }
            self._attach_action(alert_id, action)

        # 记名游客按订单归还占用
        named_now = 0
        processed = set(existing.get("processed_order_ids", [])) if existing else set()
        for order_id in order_ids:
            if order_id in processed:
                continue
            order = self.engine._order(order_id)
            before = sum(a["quantity"] for a in order.allocations
                         if a.get("route_id") == route_id
                         or (not route_id and a.get("session_id") == session_id))
            effects += self.engine._release_occupied(order, reason="evacuate")
            named_now += before
            processed.add(order_id)
        # 散客按报告总人数扣除记名实际人数后核减（有守卫，不会减成负数）
        remaining = quantity - named_now
        if remaining > 0:
            effects += self._adjust_anonymous(session_id, route_id, -remaining)
        action["processed_order_ids"] = sorted(processed)
        action["capacity_effects"] = action.get("capacity_effects", []) + effects
        self.store.record("visitors_evacuated", {
            "session_id": session_id, "route_id": route_id,
            "quantity": quantity, "order_ids": order_ids,
            "alert_id": alert_id, "merged": merged,
            "capacity_effects": effects,
        }, actor=actor, at=now)
        return action

    # ------------------------------------------------------------------
    # 恢复与重开
    # ------------------------------------------------------------------

    def recover_alert(self, alert_id: str, checks: list[str], reason: str,
                      actor: str = "safety",
                      idempotency_key: Optional[str] = None,
                      at: Optional[float] = None) -> Alert:
        """逐条核验恢复条件后解除处置并记录"为何允许重新开放"。"""
        with self.store.lock:
            if self.store.replayed(idempotency_key) is not None:
                return self._alert(alert_id)
            alert = self._alert(alert_id)
            if alert.state == AlertState.RECOVERED:
                raise StateError("告警已恢复")
            missing = [c for c in alert.recovery_conditions if c not in checks]
            if missing:
                raise StateError(f"恢复条件未全部满足，缺少: {missing}")
            now = at if at is not None else self.store.now()
            unblocked = self._unblock_targets(alert)
            alert.state = AlertState.RECOVERED
            alert.recovered_at = now
            alert.recovered_reason = reason
            alert.recovery_checks = list(checks)
            self.store.record("alert_recovered", {
                "alert_id": alert_id, "scope": alert.scope,
                "target_id": alert.target_id, "reason": reason,
                "checks": checks, "unblocked": unblocked,
            }, actor=actor, idempotency_key=idempotency_key, at=now)
            return alert

    def close_alert(self, alert_id: str, note: str = "",
                    actor: str = "safety") -> Alert:
        with self.store.lock:
            alert = self._alert(alert_id)
            alert.state = AlertState.CLOSED
            self.store.record("alert_closed", {
                "alert_id": alert_id, "note": note,
            }, actor=actor)
            return alert

    def safety_board(self) -> dict[str, Any]:
        """安全负责人看板：每条路线的风险、处置人、恢复条件与现状。"""
        with self.store.lock:
            routes = []
            for route in self.store.routes.values():
                route_alerts = [a for a in self.store.alerts.values()
                                if a.scope == "route" and a.target_id == route.id]
                open_alert = next((a for a in route_alerts
                                   if a.state == AlertState.OPEN), None)
                routes.append({
                    "route_id": route.id, "name": route.name,
                    "blocked": route.blocked, "blocked_reason": route.blocked_reason,
                    "risk": open_alert.level.value if open_alert else "none",
                    "alert_id": open_alert.id if open_alert else None,
                    "handler_id": open_alert.handler_id if open_alert else "",
                    "recovery_conditions": (
                        open_alert.recovery_conditions if open_alert else []),
                    "state": open_alert.state.value if open_alert else "normal",
                    "recovered_reason": (
                        next((a.recovered_reason for a in route_alerts
                              if a.state == AlertState.RECOVERED), "")),
                })
            alerts = [self._alert_dict(a) for a in self.store.alerts.values()]
            return {"routes": routes, "alerts": alerts}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _transfer_order_allocations(self, order, from_route_id: str,
                                    to_route_id: Optional[str]) -> list[dict]:
        effects: list[dict[str, Any]] = []
        new_allocations = []
        matched = False
        for alloc in order.allocations:
            if alloc.get("route_id") != from_route_id:
                new_allocations.append(alloc)
                continue
            matched = True
            old_session = self.engine._session(alloc["session_id"])
            old_keys = self.store.capacity_keys(
                old_session, alloc["entrance_id"], from_route_id)
            if to_route_id:
                target_session = self.engine._session(alloc["session_id"])
                self.store.assert_capacity(
                    target_session, alloc["entrance_id"], to_route_id,
                    alloc["quantity"])
                new_keys = self.store.capacity_keys(
                    target_session, alloc["entrance_id"], to_route_id)
            # route 桶可能已被散客调整先核减，按实际在途人数钳制
            applied = self.store.apply_delta(
                old_keys, occupied=-alloc["quantity"],
                clamp_scopes={"route"})
            for key, _dh, do in applied:
                effects.append({"scope": key[0], "target_id": key[1],
                                "session_id": key[1] if key[0] == "session" else key[-1],
                                "held_delta": 0, "occupied_delta": do})
            if to_route_id:
                self.store.apply_delta(new_keys, occupied=alloc["quantity"])
                effects += self.engine._effects(
                    new_keys, occupied=alloc["quantity"])
                new_allocations.append({**alloc, "route_id": to_route_id})
        if matched:
            order.allocations = new_allocations
        return effects

    @staticmethod
    def _route_occupied(order, route_id: str) -> int:
        return sum(a["quantity"] for a in order.allocations
                   if a.get("route_id") == route_id)

    def _move_anonymous(self, from_route_id: str, to_route_id: str,
                        session_id: Optional[str], quantity: int) -> list[dict]:
        effects = self._adjust_anonymous(session_id, from_route_id, -quantity)
        effects += self._adjust_anonymous(session_id, to_route_id, quantity)
        return effects

    def _adjust_anonymous(self, session_id: Optional[str], route_id: Optional[str],
                          delta: int) -> list[dict]:
        """散客人数调整：直接改占用计数，禁止减成负数。"""
        if not route_id or not session_id:
            return []
        key = ("route", route_id, session_id)
        bucket = self.store._bucket(key)
        if bucket["occupied"] + delta < 0:
            raise CapacityError(
                f"散客核减会使游线 {route_id} 占用为负: {bucket['occupied']}{delta}")
        bucket["occupied"] += delta
        return [{"scope": "route", "target_id": route_id,
                 "session_id": session_id, "held_delta": 0,
                 "occupied_delta": delta, "anonymous": True}]

    def _unblock_targets(self, alert: Alert) -> list[str]:
        unblocked = []
        if alert.scope == "route":
            route = self.store.routes.get(alert.target_id)
            if route and route.blocked:
                route.blocked = False
                route.blocked_reason = ""
                unblocked.append(route.id)
        elif alert.scope == "area":
            for route in self.store.routes.values():
                if alert.target_id in route.area_ids and route.blocked:
                    route.blocked = False
                    route.blocked_reason = ""
                    unblocked.append(route.id)
        return unblocked

    def _attach_action(self, alert_id: Optional[str], action: dict) -> None:
        if alert_id and alert_id in self.store.alerts:
            self.store.alerts[alert_id].actions.append(action)

    def _find_action(self, alert_id: Optional[str], merge_key: Optional[str],
                     action_type: str, *target_ids) -> Optional[dict]:
        if not merge_key or not alert_id:
            return None
        alert = self.store.alerts.get(alert_id)
        if alert is None:
            return None
        for action in alert.actions:
            if (action.get("merge_key") == merge_key
                    and action["type"] == action_type
                    and self._action_targets(action) == tuple(t or "" for t in target_ids)):
                return action
        return None

    @staticmethod
    def _action_targets(action: dict) -> tuple[str, ...]:
        if action["type"] == "restrict_route":
            return (action.get("route_id") or "",)
        if action["type"] == "transfer_visitors":
            return (action.get("from_route_id") or "",
                    action.get("to_route_id") or "")
        return (action.get("route_id") or action.get("session_id") or "",)

    def _validate_target(self, scope: str, target_id: str) -> None:
        if scope == "route" and target_id not in self.store.routes:
            raise NotFoundError(f"游线不存在: {target_id}")
        if scope == "area" and target_id not in self.store.areas:
            raise NotFoundError(f"区域不存在: {target_id}")
        if scope == "facility" and target_id not in self.store.facilities:
            raise NotFoundError(f"设施不存在: {target_id}")
        if scope == "session" and target_id not in self.store.sessions:
            raise NotFoundError(f"场次不存在: {target_id}")

    def _alert(self, alert_id: str) -> Alert:
        alert = self.store.alerts.get(alert_id)
        if alert is None:
            raise NotFoundError(f"告警不存在: {alert_id}")
        return alert

    def _alert_by_target(self, scope: str, target_id: str) -> Alert:
        for alert in self.store.alerts.values():
            if alert.scope == scope and alert.target_id == target_id:
                return alert
        raise NotFoundError(f"{scope}:{target_id} 无告警")

    @staticmethod
    def _alert_dict(alert: Alert) -> dict[str, Any]:
        return {
            "id": alert.id, "level": alert.level.value,
            "scope": alert.scope, "target_id": alert.target_id,
            "title": alert.title, "detail": alert.detail,
            "state": alert.state.value, "handler_id": alert.handler_id,
            "recovery_conditions": alert.recovery_conditions,
            "actions": alert.actions,
            "recovered_reason": alert.recovered_reason,
            "recovery_checks": alert.recovery_checks,
            "created_at": alert.created_at, "recovered_at": alert.recovered_at,
        }

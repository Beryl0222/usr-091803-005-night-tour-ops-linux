"""核心调度引擎：容量、锁位、订单与规则快照。

并发正确性只有一条规则：任何"检查容量 -> 计数 -> 写日志"的序列都必须在
``store.lock`` 内完成，因此渠道并发锁位不可能共同突破区域上限。

退款/改期/补偿一律读取订单上的 ``policy_snapshot``（购票时锁定），
运营事后调整规则不溯及既往。
"""

from __future__ import annotations

from typing import Any, Optional

from .models import (
    DEFAULT_POLICY,
    Disposition,
    Entrance,
    Facility,
    FacilityState,
    Hold,
    HoldState,
    LedgerEntry,
    Merchant,
    Order,
    OrderItem,
    OrderStatus,
    Area,
    Route,
    RulePolicy,
    Session,
    SessionState,
    TicketKind,
)
from .store import CapacityError, NotFoundError, StateError, Store, session_capacity


class RuleError(Exception):
    """诉求不符合购票时规则（如开场后不可退、改期容量不足）。"""


class Engine:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    # 资源注册
    # ------------------------------------------------------------------

    def add_area(self, area_id: str, name: str, capacity: int,
                 kind: str = "general", backup_area_id: Optional[str] = None) -> Area:
        with self.store.lock:
            if area_id in self.store.areas:
                raise StateError(f"区域已存在: {area_id}")
            area = Area(id=area_id, name=name, capacity=capacity, kind=kind,
                        backup_area_id=backup_area_id)
            self.store.areas[area_id] = area
            self.store.record("area_registered", {
                "area_id": area_id, "name": name, "capacity": capacity,
                "kind": kind, "backup_area_id": backup_area_id,
            }, actor="admin")
            return area

    def add_entrance(self, entrance_id: str, name: str, area_id: str,
                     quotas: Optional[dict[str, int]] = None) -> Entrance:
        with self.store.lock:
            if area_id not in self.store.areas:
                raise NotFoundError(f"区域不存在: {area_id}")
            entrance = Entrance(id=entrance_id, name=name, area_id=area_id,
                                quotas=dict(quotas or {}))
            self.store.entrances[entrance_id] = entrance
            self.store.record("entrance_registered", {
                "entrance_id": entrance_id, "name": name, "area_id": area_id,
                "quotas": entrance.quotas,
            }, actor="admin")
            return entrance

    def set_entrance_quota(self, entrance_id: str, session_id: str, quota: int) -> None:
        """同一场次拆到多个入口：按入口设置分时配额。"""
        with self.store.lock:
            entrance = self._entrance(entrance_id)
            session = self._session(session_id)
            if quota < 0:
                raise RuleError("配额不能为负")
            entrance.quotas[session_id] = quota
            if entrance_id not in session.entrance_ids:
                session.entrance_ids.append(entrance_id)
            self.store.record("entrance_quota_set", {
                "entrance_id": entrance_id, "session_id": session_id, "quota": quota,
            }, actor="admin")

    def add_route(self, route_id: str, name: str, capacity: int,
                  area_ids: Optional[list[str]] = None) -> Route:
        with self.store.lock:
            route = Route(id=route_id, name=name, capacity=capacity,
                          area_ids=list(area_ids or []))
            self.store.routes[route_id] = route
            self.store.record("route_registered", {
                "route_id": route_id, "name": name, "capacity": capacity,
                "area_ids": route.area_ids,
            }, actor="admin")
            return route

    def add_facility(self, facility_id: str, name: str, area_id: str) -> Facility:
        with self.store.lock:
            if area_id not in self.store.areas:
                raise NotFoundError(f"区域不存在: {area_id}")
            facility = Facility(id=facility_id, name=name, area_id=area_id)
            self.store.facilities[facility_id] = facility
            self.store.areas[area_id].facility_ids.append(facility_id)
            self.store.record("facility_registered", {
                "facility_id": facility_id, "name": name, "area_id": area_id,
            }, actor="admin")
            return facility

    def set_facility_state(self, facility_id: str, state: FacilityState,
                           detail: str = "", actor: str = "ops",
                           idempotency_key: Optional[str] = None) -> Facility:
        with self.store.lock:
            if (replayed := self.store.replayed(idempotency_key)) is not None:
                return self.store.facilities[facility_id]
            facility = self.store.facilities.get(facility_id)
            if facility is None:
                raise NotFoundError(f"设施不存在: {facility_id}")
            old = facility.state
            facility.state = FacilityState(state) if not isinstance(state, FacilityState) else state
            facility.detail = detail
            self.store.record("facility_state_changed", {
                "facility_id": facility_id, "area_id": facility.area_id,
                "old_state": old.value, "new_state": facility.state.value, "detail": detail,
            }, actor=actor, idempotency_key=idempotency_key)
            return facility

    def add_merchant(self, merchant_id: str, name: str,
                     default_lead_rate: float = 0.0) -> Merchant:
        with self.store.lock:
            merchant = Merchant(id=merchant_id, name=name,
                                default_lead_rate=default_lead_rate)
            self.store.merchants[merchant_id] = merchant
            self.store.record("merchant_registered", {
                "merchant_id": merchant_id, "name": name,
                "default_lead_rate": default_lead_rate,
            }, actor="admin")
            return merchant

    def create_session(self, session_id: str, title: str, area_id: str,
                       kind: TicketKind, start_at: float, end_at: float,
                       entrance_ids: Optional[list[str]] = None,
                       route_ids: Optional[list[str]] = None,
                       policy: Optional[RulePolicy] = None,
                       capacity_override: Optional[int] = None,
                       reopen_conditions: Optional[list[str]] = None) -> Session:
        with self.store.lock:
            if area_id not in self.store.areas:
                raise NotFoundError(f"区域不存在: {area_id}")
            if session_id in self.store.sessions:
                raise StateError(f"场次已存在: {session_id}")
            kind = kind if isinstance(kind, TicketKind) else TicketKind(kind)
            session = Session(
                id=session_id, title=title, area_id=area_id, kind=kind,
                start_at=start_at, end_at=end_at,
                policy=policy or DEFAULT_POLICY,
                entrance_ids=list(entrance_ids or []),
                route_ids=list(route_ids or []),
                reopen_conditions=list(reopen_conditions or []),
            )
            self.store.sessions[session_id] = session
            area = self.store.areas[area_id]
            if capacity_override is not None:
                area.session_capacity[session_id] = capacity_override
            for entrance_id in session.entrance_ids:
                entrance = self._entrance(entrance_id)
                entrance.quotas.setdefault(session_id, 0)
            self.store.record("session_created", {
                "session_id": session_id, "title": title, "area_id": area_id,
                "kind": kind.value, "start_at": start_at, "end_at": end_at,
                "entrance_ids": session.entrance_ids, "route_ids": session.route_ids,
                "capacity": session_capacity(session, area),
                "policy": session.policy.to_dict(),
                "reopen_conditions": session.reopen_conditions,
            }, actor="admin")
            return session

    # ------------------------------------------------------------------
    # 锁位与支付
    # ------------------------------------------------------------------

    def create_hold(self, session_id: str, entrance_id: str, quantity: int,
                    channel: str, ttl_seconds: int = 120,
                    route_id: Optional[str] = None,
                    hold_id: Optional[str] = None,
                    idempotency_key: Optional[str] = None,
                    at: Optional[float] = None) -> Hold:
        """渠道并发锁位：四维度容量在同一把锁内原子校验。"""
        if quantity <= 0:
            raise RuleError("锁位数量必须为正")
        with self.store.lock:
            replayed = self.store.replayed(idempotency_key)
            if replayed is not None:
                return self._hold_from_replay(replayed, idempotency_key)
            session = self._session(session_id)
            if session.state != SessionState.SCHEDULED:
                raise StateError(f"场次当前状态 {session.state.value}，不接受锁位")
            entrance = self._entrance(entrance_id)
            if not entrance.active:
                raise StateError(f"入口已停用: {entrance_id}")
            if route_id:
                route = self._route(route_id)
                if route.blocked or not route.active:
                    raise StateError(f"游线限流/阻断中: {route_id}")
            keys = self.store.capacity_keys(session, entrance_id, route_id)
            self.store.assert_capacity(session, entrance_id, route_id, quantity)
            now = at if at is not None else self.store.now()
            hold = Hold(
                id=hold_id or self.store.next_id("hold"),
                session_id=session_id, entrance_id=entrance_id,
                route_id=route_id, quantity=quantity, channel=channel,
                expires_at=now + ttl_seconds, created_at=now,
            )
            self.store.apply_delta(keys, held=quantity)
            self.store.holds[hold.id] = hold
            self.store.record("hold_created", {
                "hold_id": hold.id, "session_id": session_id,
                "entrance_id": entrance_id, "route_id": route_id,
                "quantity": quantity, "channel": channel,
                "expires_at": hold.expires_at,
                "capacity_effects": self._effects(keys, held=quantity),
            }, actor=channel, idempotency_key=idempotency_key, at=now)
            return hold

    def release_hold(self, hold_id: str, reason: str = "cancelled",
                     actor: str = "channel",
                     idempotency_key: Optional[str] = None) -> Hold:
        with self.store.lock:
            replayed = self.store.replayed(idempotency_key)
            if replayed is not None:
                return self.store.holds[hold_id]
            hold = self._hold(hold_id)
            if hold.state != HoldState.HELD:
                raise StateError(f"锁位状态为 {hold.state.value}，不可释放")
            session = self._session(hold.session_id)
            keys = self.store.capacity_keys(session, hold.entrance_id, hold.route_id)
            self.store.apply_delta(keys, held=-hold.quantity)
            hold.state = HoldState.RELEASED
            self.store.record("hold_released", {
                "hold_id": hold_id, "session_id": hold.session_id,
                "entrance_id": hold.entrance_id, "quantity": hold.quantity,
                "reason": reason,
                "capacity_effects": self._effects(keys, held=-hold.quantity),
            }, actor=actor, idempotency_key=idempotency_key)
            return hold

    def sweep_expired_holds(self) -> list[str]:
        """清理超时未支付的锁位（可由定时任务调用）。"""
        now = self.store.now()
        released = []
        with self.store.lock:
            expired = [h.id for h in self.store.holds.values()
                       if h.state == HoldState.HELD and h.expires_at <= now]
        for hold_id in expired:
            self.release_hold(hold_id, reason="ttl_expired", actor="system")
            released.append(hold_id)
        return released

    def confirm_order(self, hold_id: str, visitor_id: str,
                      items: list[dict[str, Any]], channel: str = "channel",
                      order_id: Optional[str] = None,
                      idempotency_key: Optional[str] = None,
                      at: Optional[float] = None) -> Order:
        """锁位转正式订单：held 计数转 occupied，并冻结购票时规则快照。

        items: [{kind, quantity, unit_price, merchant_id?, lead_rate?}]，
        各行 quantity 之和必须等于锁位数量；同一订单内各行共享该锁位容量。
        """
        with self.store.lock:
            replayed = self.store.replayed(idempotency_key)
            if replayed is not None:
                oid = replayed.data["order_id"]
                return self.store.orders[oid]
            hold = self._hold(hold_id)
            if hold.state != HoldState.HELD:
                raise StateError(f"锁位状态为 {hold.state.value}，不可确认")
            now = at if at is not None else self.store.now()
            if hold.expires_at <= now:
                self.release_hold(hold_id, reason="ttl_expired", actor="system")
                raise StateError("锁位已超时并释放")
            session = self._session(hold.session_id)
            order_items = self._build_items(items, hold, session)
            total_qty = sum(item.quantity for item in order_items)
            if total_qty != hold.quantity:
                raise RuleError(
                    f"订单行数量合计 {total_qty} 与锁位数量 {hold.quantity} 不一致"
                )
            paid = sum(item.quantity * item.unit_price for item in order_items)
            order = Order(
                id=order_id or self.store.next_id("order"),
                visitor_id=visitor_id, items=order_items,
                status=OrderStatus.PAID, created_at=now,
                policy_snapshot={session.id: session.policy.to_dict()},
                paid_amount=paid, channel=channel, hold_ids=[hold_id],
            )
            keys = self.store.capacity_keys(session, hold.entrance_id, hold.route_id)
            self.store.apply_delta(keys, held=-hold.quantity, occupied=hold.quantity)
            hold.state = HoldState.CONFIRMED
            hold.order_id = order.id
            order.allocations = [{
                "session_id": session.id,
                "entrance_id": hold.entrance_id,
                "route_id": hold.route_id,
                "quantity": hold.quantity,
            }]
            self.store.orders[order.id] = order
            self._ledger_income(order, now)
            self.store.record("order_confirmed", {
                "order_id": order.id, "hold_id": hold_id,
                "session_id": session.id, "visitor_id": visitor_id,
                "entrance_id": hold.entrance_id, "route_id": hold.route_id,
                "quantity": hold.quantity, "paid_amount": paid,
                "items": [self._item_dict(i) for i in order_items],
                "policy_snapshot": order.policy_snapshot,
                "capacity_effects": self._effects(
                    keys, held=-hold.quantity, occupied=hold.quantity),
            }, actor=channel, idempotency_key=idempotency_key, at=now)
            return order

    def checkout_order(self, order_id: str, actor: str = "gate",
                       idempotency_key: Optional[str] = None) -> Order:
        """游客离场/批次清场：占用容量归还。"""
        with self.store.lock:
            replayed = self.store.replayed(idempotency_key)
            if replayed is not None:
                return self.store.orders[order_id]
            order = self._order(order_id)
            if order.status in (OrderStatus.REFUNDED, OrderStatus.VOID):
                raise StateError("订单已退/作废，无需离场")
            effects = self._release_occupied(order, reason="checkout")
            self.store.record("order_checked_out", {
                "order_id": order_id,
                "session_id": order.items[0].session_id if order.items else None,
                "session_ids": sorted({i.session_id for i in order.items}),
                "capacity_effects": effects,
            }, actor=actor, idempotency_key=idempotency_key)
            return order

    # ------------------------------------------------------------------
    # 退款 / 改期 / 补偿（全部以购票时快照为准）
    # ------------------------------------------------------------------

    def evaluate_refund(self, order_id: str, at: Optional[float] = None,
                        operator_reason: str = "visitor_request") -> dict[str, Any]:
        """只计算不执行：返回按快照规则得出的退款方案与依据。"""
        with self.store.lock:
            order = self._order(order_id)
            at = at if at is not None else self.store.now()
            return self._refund_plan(order, at, operator_reason)

    def refund_order(self, order_id: str, reason: str = "visitor_request",
                     actor: str = "ops", ratio_override: Optional[float] = None,
                     idempotency_key: Optional[str] = None,
                     at: Optional[float] = None) -> dict[str, Any]:
        with self.store.lock:
            replayed = self.store.replayed(idempotency_key)
            if replayed is not None:
                return replayed.data["result"]
            order = self._order(order_id)
            now = at if at is not None else self.store.now()
            plan = self._refund_plan(order, now, reason, ratio_override)
            if plan["amount"] <= 0 and plan["disposition"] != Disposition.PARTIAL_REFUND.value:
                raise RuleError(plan["basis"])
            self._execute_refund(order, plan, reason, actor, now,
                                 idempotency_key)
            return plan

    def reschedule_order(self, order_id: str, to_session_id: str,
                         entrance_id: str, route_id: Optional[str] = None,
                         actor: str = "ops",
                         idempotency_key: Optional[str] = None,
                         at: Optional[float] = None) -> Order:
        """免费/付费改期：按快照政策计费，并在新场次原子占容。"""
        with self.store.lock:
            replayed = self.store.replayed(idempotency_key)
            if replayed is not None:
                return self.store.orders[replayed.data["order_id"]]
            order = self._order(order_id)
            now = at if at is not None else self.store.now()
            if order.status not in (OrderStatus.PAID, OrderStatus.COMPENSATED):
                raise StateError(f"订单状态 {order.status.value} 不允许改期")
            old_session_id = order.items[0].session_id
            old_session = self._session(old_session_id)
            new_session = self._session(to_session_id)
            if new_session.state != SessionState.SCHEDULED:
                raise StateError(f"目标场次状态 {new_session.state.value}，不可改入")
            policy = self._snapshot_policy(order, old_session_id)
            fee = 0 if policy.free_reschedule else policy.reschedule_fee
            quantity = sum(i.quantity for i in order.items)
            # 先校验新场次容量，再动老场次占用
            self.store.assert_capacity(new_session, entrance_id, route_id, quantity)
            old_effects = self._release_occupied(order, reason="reschedule")
            new_keys = self.store.capacity_keys(new_session, entrance_id, route_id)
            self.store.apply_delta(new_keys, occupied=quantity)
            new_effects = self._effects(new_keys, occupied=quantity)
            for item in order.items:
                item.session_id = to_session_id
            order.allocations = [{
                "session_id": to_session_id, "entrance_id": entrance_id,
                "route_id": route_id, "quantity": quantity,
            }]
            order.policy_snapshot.setdefault(
                to_session_id, new_session.policy.to_dict()
            )
            record = {
                "from_session_id": old_session_id, "to_session_id": to_session_id,
                "entrance_id": entrance_id, "route_id": route_id,
                "fee": fee, "at": now,
            }
            order.reschedules.append(record)
            if fee:
                self._ledger(LedgerEntry(
                    id=self.store.next_id("led"), at=now,
                    kind=new_session.kind, direction="reschedule_fee",
                    amount=fee, session_id=to_session_id, order_id=order.id,
                    memo="改期手续费",
                ))
            self.store.record("order_rescheduled", {
                "order_id": order_id, "result": record,
                "from_session_id": old_session_id,
                "to_session_id": to_session_id,
                "entrance_id": entrance_id, "route_id": route_id,
                "quantity": quantity, "fee": fee,
                "capacity_effects": old_effects + new_effects,
            }, actor=actor, idempotency_key=idempotency_key, at=now)
            return order

    def grant_compensation(self, order_id: str, amount: int, memo: str,
                           actor: str = "ops",
                           idempotency_key: Optional[str] = None,
                           at: Optional[float] = None) -> dict[str, Any]:
        with self.store.lock:
            replayed = self.store.replayed(idempotency_key)
            if replayed is not None:
                return replayed.data["compensation"]
            order = self._order(order_id)
            now = at if at is not None else self.store.now()
            comp = {"amount": amount, "memo": memo, "at": now}
            order.compensations.append(comp)
            if order.status == OrderStatus.PAID:
                order.status = OrderStatus.COMPENSATED
            for item in order.items:
                self._ledger(LedgerEntry(
                    id=self.store.next_id("led"), at=now, kind=item.kind,
                    direction="compensation", amount=amount // max(1, len(order.items)),
                    session_id=item.session_id, order_id=order.id,
                    merchant_id=item.merchant_id, memo=memo,
                ))
            self.store.record("compensation_granted", {
                "order_id": order_id, "compensation": comp,
                "session_id": order.items[0].session_id,
            }, actor=actor, idempotency_key=idempotency_key, at=now)
            return comp

    # ------------------------------------------------------------------
    # 场次状态：暂停 / 恢复 / 取消 / 迁移 / 结束
    # ------------------------------------------------------------------

    def suspend_session(self, session_id: str, reason: str, actor: str = "safety",
                        idempotency_key: Optional[str] = None) -> Session:
        """暴雨预警等：立即停止新锁位，已售订单不动。"""
        with self.store.lock:
            if self.store.replayed(idempotency_key) is not None:
                return self._session(session_id)
            session = self._session(session_id)
            if session.state not in (SessionState.SCHEDULED,):
                raise StateError(f"场次状态 {session.state.value}，不可暂停")
            session.state = SessionState.SUSPENDED
            session.suspended_reason = reason
            self.store.record("session_suspended", {
                "session_id": session_id, "reason": reason,
                "reopen_conditions": session.reopen_conditions,
            }, actor=actor, idempotency_key=idempotency_key)
            return session

    def resume_session(self, session_id: str, recovery_checks: list[str],
                       reason: str, actor: str = "safety",
                       idempotency_key: Optional[str] = None) -> Session:
        """满足全部恢复条件并逐条留证后才允许重新开放。"""
        with self.store.lock:
            if self.store.replayed(idempotency_key) is not None:
                return self._session(session_id)
            session = self._session(session_id)
            if session.state != SessionState.SUSPENDED:
                raise StateError(f"场次状态 {session.state.value}，无需恢复")
            missing = [c for c in session.reopen_conditions if c not in recovery_checks]
            if missing:
                raise RuleError(f"恢复条件尚未全部满足，缺少: {missing}")
            # 相关设施不得处于安全关闭/故障
            blockers = []
            area = self.store.areas.get(session.area_id)
            if area:
                for fid in area.facility_ids:
                    facility = self.store.facilities.get(fid)
                    if facility and facility.state in (
                        FacilityState.SAFETY_CLOSED, FacilityState.FAULT,
                    ):
                        blockers.append(f"{facility.id}:{facility.state.value}")
            if blockers:
                raise RuleError(f"设施未恢复，禁止重开: {blockers}")
            session.state = SessionState.SCHEDULED
            session.note = reason
            self.store.record("session_resumed", {
                "session_id": session_id, "reason": reason,
                "recovery_checks": recovery_checks,
                "capacity_after": self.store.capacity_view(session),
            }, actor=actor, idempotency_key=idempotency_key)
            return session

    def cancel_session(self, session_id: str, reason: str, actor: str = "ops",
                       auto_refund: bool = True,
                       idempotency_key: Optional[str] = None,
                       at: Optional[float] = None) -> dict[str, Any]:
        """取消（不迁移）：释放未支付锁位；已售订单按购票时规则批量退款。

        已在园游客的占用容量待现场疏散上报后释放（见 safety 模块）。
        """
        with self.store.lock:
            replayed = self.store.replayed(idempotency_key)
            if replayed is not None:
                return replayed.data["result"]
            session = self._session(session_id)
            if session.state in (SessionState.CANCELLED, SessionState.CLOSED,
                                 SessionState.MIGRATED):
                raise StateError(f"场次已终态: {session.state.value}")
            now = at if at is not None else self.store.now()
            released_holds, hold_effects = self._release_session_holds(session)
            session.state = SessionState.CANCELLED
            session.cancel_reason = reason
            results = []
            refund_effects: list[dict[str, Any]] = []
            if auto_refund:
                for order in list(self.store.orders.values()):
                    if not any(i.session_id == session_id for i in order.items):
                        continue
                    if order.status not in (OrderStatus.PAID, OrderStatus.COMPENSATED):
                        continue
                    plan = self._refund_plan(order, now, "operator_cancel")
                    refund_effects += self._execute_refund(
                        order, plan, "operator_cancel", actor, now
                    )
                    results.append({"order_id": order.id, **plan})
            result = {
                "session_id": session_id, "state": session.state.value,
                "released_holds": released_holds, "refunds": results,
                "reason": reason,
                "capacity_effects": hold_effects + refund_effects,
            }
            self.store.record("session_cancelled", {
                **result, "result": {k: v for k, v in result.items()
                                     if k != "capacity_effects"},
            }, actor=actor, idempotency_key=idempotency_key, at=now)
            return result

    def migrate_session(self, session_id: str, backup_area_id: str,
                        new_start_at: float, new_end_at: float,
                        entrance_ids: list[str], route_ids: Optional[list[str]] = None,
                        title: Optional[str] = None,
                        actor: str = "ops",
                        idempotency_key: Optional[str] = None,
                        at: Optional[float] = None) -> dict[str, Any]:
        """取消原址场次并迁往备用区域：容量原子搬运，装不下的订单按规则退款。

        迁移成功的订单保留有效，按快照规则发放补偿权益；游客也可事后
        另行申请退款（仍按自己的快照计算）。
        """
        with self.store.lock:
            replayed = self.store.replayed(idempotency_key)
            if replayed is not None:
                new_id = replayed.data["new_session_id"]
                return self._migration_result(new_id)
            session = self._session(session_id)
            if session.state in (SessionState.CANCELLED, SessionState.MIGRATED,
                                 SessionState.CLOSED):
                raise StateError(f"场次已终态: {session.state.value}")
            backup = self.store.areas.get(backup_area_id)
            if backup is None:
                raise NotFoundError(f"备用区域不存在: {backup_area_id}")
            now = at if at is not None else self.store.now()
            new_id = f"{session_id}->backup@{self.store.next_seq():06d}"
            new_session = Session(
                id=new_id,
                title=title or f"{session.title}（迁移场）",
                area_id=backup_area_id, kind=session.kind,
                start_at=new_start_at, end_at=new_end_at,
                state=SessionState.SCHEDULED,
                policy=session.policy,  # 迁移沿用原批次规则
                entrance_ids=list(entrance_ids), route_ids=list(route_ids or []),
                migrated_from=session_id,
                reopen_conditions=list(session.reopen_conditions),
            )
            self.store.sessions[new_id] = new_session

            # 1) 老场次锁位全部释放（渠道需按新场次重新锁位）
            released_holds, hold_effects = self._release_session_holds(session)
            capacity_effects: list[dict[str, Any]] = list(hold_effects)

            # 入口必须服务于备用区域；未显式配配额时默认取备用区容量
            for entrance_id in entrance_ids:
                entrance = self._entrance(entrance_id)
                if entrance.area_id != backup_area_id:
                    raise StateError(
                        f"入口 {entrance_id} 不属于备用区域 {backup_area_id}，"
                        "迁移请使用备用区域入口")
                entrance.quotas.setdefault(new_id, backup.capacity)

            # 2) 已售订单按新区域容量逐单搬运；容量不足者按取消规则退款
            migrated, refunded = [], []
            orders = [o for o in self.store.orders.values()
                      if any(i.session_id == session_id for i in o.items)
                      and o.status in (OrderStatus.PAID, OrderStatus.COMPENSATED)]
            for order in orders:
                quantity = sum(i.quantity for i in order.items
                               if i.session_id == session_id)
                try:
                    effects = self._move_order_to_session(
                        order, session, new_session, quantity, entrance_ids[0],
                        (route_ids or [None])[0], now,
                    )
                except CapacityError:
                    # 装不下：原址场次也已终止，占用当场归还（游客疏散由现场处置）
                    effects = self._release_occupied(order, reason="migrate_refund")
                    plan = self._refund_plan(order, now, "operator_cancel")
                    self._execute_refund(order, plan, "migrate_capacity_shortfall",
                                         actor, now)
                    refunded.append({"order_id": order.id, **plan})
                    capacity_effects += effects
                    continue
                policy = self._snapshot_policy(order, session_id)
                comp = None
                if policy.compensation_on_migrate and policy.compensation_amount > 0:
                    comp = {"amount": policy.compensation_amount,
                            "memo": "场次迁移补偿", "at": now}
                    order.compensations.append(comp)
                    order.status = OrderStatus.COMPENSATED
                    for item in order.items:
                        self._ledger(LedgerEntry(
                            id=self.store.next_id("led"), at=now, kind=item.kind,
                            direction="compensation",
                            amount=policy.compensation_amount // max(1, len(order.items)),
                            session_id=new_id, order_id=order.id,
                            merchant_id=item.merchant_id, memo="场次迁移补偿",
                        ))
                migrated.append({"order_id": order.id, "quantity": quantity,
                                 "compensation": comp})
                capacity_effects += effects

            session.state = SessionState.MIGRATED
            session.migrated_to = new_id
            self.store.record("session_migrated", {
                "session_id": session_id, "new_session_id": new_id,
                "from_session_id": session_id,
                "backup_area_id": backup_area_id,
                "new_session": {
                    "id": new_id, "title": new_session.title,
                    "area_id": backup_area_id, "kind": session.kind.value,
                    "start_at": new_start_at, "end_at": new_end_at,
                    "entrance_ids": list(entrance_ids),
                    "route_ids": list(route_ids or []),
                    "migrated_from": session_id,
                    "policy": session.policy.to_dict(),
                    "reopen_conditions": list(session.reopen_conditions),
                },
                "released_holds": released_holds,
                "migrated": migrated, "refunded": refunded,
                "capacity_effects": capacity_effects,
            }, actor=actor, idempotency_key=idempotency_key, at=now)
            return self._migration_result(new_id, {
                "released_holds": released_holds,
                "migrated": migrated, "refunded": refunded,
            })

    def end_session(self, session_id: str, actor: str = "ops",
                    force: bool = False) -> dict[str, Any]:
        """正常结束批次：默认要求占用已清空（人已离场/疏散完毕）。"""
        with self.store.lock:
            session = self._session(session_id)
            view = self.store.capacity_view(session)
            remaining = view["session"]["occupied"]
            if remaining and not force:
                raise StateError(f"仍有 {remaining} 人在场，需清场后结束或 force=True")
            session.state = SessionState.CLOSED
            self.store.record("session_ended", {
                "session_id": session_id, "forced": force,
                "remaining_occupied": remaining,
                "capacity_final": view,
            }, actor=actor)
            return {"session_id": session_id, "remaining_occupied": remaining}

    # ------------------------------------------------------------------
    # 内部：订单/容量/台账/规则计算
    # ------------------------------------------------------------------

    def _move_order_to_session(self, order: Order, old: Session, new: Session,
                               quantity: int, entrance_id: str,
                               route_id: Optional[str], now: float) -> list[dict]:
        """把订单占用整体搬到新场次（调用方须先通过新场次容量校验）。"""
        self.store.assert_capacity(new, entrance_id, route_id, quantity)
        effects = self._release_occupied(order, reason="migrate")
        keys = self.store.capacity_keys(new, entrance_id, route_id)
        self.store.apply_delta(keys, occupied=quantity)
        effects += self._effects(keys, occupied=quantity)
        for item in order.items:
            if item.session_id == old.id:
                item.session_id = new.id
        order.allocations = [{
            "session_id": new.id, "entrance_id": entrance_id,
            "route_id": route_id, "quantity": quantity,
        }]
        order.policy_snapshot[new.id] = new.policy.to_dict()
        order.reschedules.append({
            "from_session_id": old.id, "to_session_id": new.id,
            "entrance_id": entrance_id, "route_id": route_id,
            "fee": 0, "at": now, "kind": "migration",
        })
        return effects

    def _release_occupied(self, order: Order, reason: str) -> list[dict]:
        """按订单 allocations 精确归还当前占用，并清空分配。

        route 维度可能已被现场限流/转移（散客）先行核减，释放时按桶内
        实际在途人数钳制；其余维度与订单一一对应，不做钳制。
        """
        effects: list[dict] = []
        for alloc in order.allocations:
            session = self._session(alloc["session_id"])
            keys = self.store.capacity_keys(
                session, alloc["entrance_id"], alloc.get("route_id")
            )
            qty = alloc["quantity"]
            applied = self.store.apply_delta(
                keys, occupied=-qty, clamp_scopes={"route"})
            for key, _dh, do in applied:
                scope, target = key[0], key[1]
                effect = {"scope": scope, "target_id": target,
                          "held_delta": 0, "occupied_delta": do,
                          "requested_delta": -qty if scope == "route" else do}
                effect["session_id"] = target if scope == "session" else key[-1]
                if do != -qty:
                    effect["clamped"] = True
                    effect["note"] = "现场动作已先行核减在途人数"
                effects.append(effect)
        order.allocations = []
        return effects

    def _release_session_holds(self, session: Session) -> tuple[list[str], list[dict]]:
        released = []
        effects: list[dict[str, Any]] = []
        for hold in list(self.store.holds.values()):
            if hold.session_id == session.id and hold.state == HoldState.HELD:
                keys = self.store.capacity_keys(session, hold.entrance_id,
                                                hold.route_id)
                self.store.apply_delta(keys, held=-hold.quantity)
                effects += self._effects(keys, held=-hold.quantity)
                hold.state = HoldState.RELEASED
                released.append(hold.id)
        return released, effects

    @staticmethod
    def _effects(keys: list[tuple], held: int = 0,
                 occupied: int = 0) -> list[dict[str, Any]]:
        """把一次计数变动展开为可审计、可重放的标准化容量效果。"""
        out = []
        for scope, target, *rest in keys:
            effect = {"scope": scope, "target_id": target,
                      "held_delta": held, "occupied_delta": occupied}
            if scope == "session":
                effect["session_id"] = target
            elif rest:
                effect["session_id"] = rest[-1]
            out.append(effect)
        return out

    def _refund_plan(self, order: Order, at: float, reason: str,
                     ratio_override: Optional[float] = None) -> dict[str, Any]:
        lines = []
        total = 0
        paid_total = 0
        basis_parts = []
        for item in order.items:
            session = self._session(item.session_id)
            policy = self._snapshot_policy(order, item.session_id)
            price = item.quantity * item.unit_price
            paid_total += price
            ratio, why = self._refund_ratio(policy, session, at, reason)
            if ratio_override is not None:
                ratio, why = ratio_override, "运营裁定比例"
            amount = int(price * ratio)
            total += amount
            lines.append({
                "kind": item.kind.value, "session_id": item.session_id,
                "quantity": item.quantity, "unit_price": item.unit_price,
                "ratio": ratio, "amount": amount, "basis": why,
                "merchant_id": item.merchant_id,
            })
            basis_parts.append(f"{item.kind.value}:{why}")
        if total <= 0:
            disposition = Disposition.UNCHANGED.value
        elif total >= paid_total:
            disposition = Disposition.REFUND.value
        else:
            disposition = Disposition.PARTIAL_REFUND.value
        return {
            "order_id": order.id, "disposition": disposition,
            "amount": total, "lines": lines,
            "basis": "；".join(basis_parts),
            "rule_locked_at": "购票时快照",
        }

    @staticmethod
    def _refund_ratio(policy: RulePolicy, session: Session, at: float,
                      reason: str) -> tuple[float, str]:
        """决定退款比例。reason: visitor_request / operator_cancel / ..."""
        started = at >= session.start_at
        if reason == "operator_cancel":
            if started:
                return (policy.after_start_refund_ratio,
                        f"园区原因且已开场，按快照退 {policy.after_start_refund_ratio:.0%}")
            return 1.0, "园区原因取消且未开场，全额退"
        # 游客主动
        if at >= session.start_at:
            return 0.0, "开场后游客主动申请，快照规则不退"
        if at >= session.start_at - policy.refund_cutoff_minutes * 60:
            return (policy.late_refund_ratio,
                    f"开场前 {policy.refund_cutoff_minutes} 分钟内，按快照退 "
                    f"{policy.late_refund_ratio:.0%}")
        return 1.0, f"早于开场前 {policy.refund_cutoff_minutes} 分钟，全额退"

    def _execute_refund(self, order: Order, plan: dict[str, Any], reason: str,
                        actor: str, now: float,
                        idempotency_key: Optional[str] = None) -> list[dict]:
        effects: list[dict[str, Any]] = []
        if plan["amount"] > 0:
            # 退款同时归还仍在占的容量（取消时人可能在场，由疏散另行处理；
            # 未开场的退款直接释放占用）。
            if reason != "migrate_capacity_shortfall":
                effects += self._release_occupied_if_before_start(order, now)
            for line in plan["lines"]:
                if line["amount"] <= 0:
                    continue
                self._ledger(LedgerEntry(
                    id=self.store.next_id("led"), at=now,
                    kind=TicketKind(line["kind"]), direction="refund",
                    amount=line["amount"], session_id=line["session_id"],
                    order_id=order.id, merchant_id=line.get("merchant_id"),
                    memo=f"退款:{reason}:{line['basis']}",
                ))
        record = {**plan, "reason": reason, "at": now}
        order.refunds.append(record)
        if plan["amount"] > 0:
            order.status = OrderStatus.REFUNDED
        self.store.record("order_refunded", {
            "order_id": order.id, "result": plan, "reason": reason,
            "session_id": order.items[0].session_id,
            "capacity_effects": effects,
        }, actor=actor, idempotency_key=idempotency_key, at=now)
        return effects

    def _release_occupied_if_before_start(self, order: Order,
                                          now: float) -> list[dict]:
        # 未开场：订单退款即释放占用；已开场：保留占用，等待疏散/离场
        sessions = [self._session(i.session_id) for i in order.items]
        if all(now < s.start_at for s in sessions):
            return self._release_occupied(order, reason="refund")
        return []

    def _build_items(self, raw: list[dict[str, Any]], hold: Hold,
                     session: Session) -> list[OrderItem]:
        if not raw:
            raise RuleError("订单至少需要一行")
        items = []
        for data in raw:
            kind = data["kind"] if isinstance(data["kind"], TicketKind) else TicketKind(data["kind"])
            sid = data.get("session_id", session.id)
            if sid != session.id:
                raise RuleError("锁位场次与订单行场次不一致")
            merchant_id = data.get("merchant_id")
            if kind == TicketKind.MERCHANT_LEAD and not merchant_id:
                raise RuleError("商户引流收入必须指定 merchant_id")
            if merchant_id and merchant_id not in self.store.merchants:
                raise NotFoundError(f"商户不存在: {merchant_id}")
            rate = data.get("lead_rate")
            if rate is None and merchant_id:
                rate = self.store.merchants[merchant_id].default_lead_rate
            items.append(OrderItem(
                kind=kind, session_id=sid, quantity=int(data["quantity"]),
                unit_price=int(data["unit_price"]), merchant_id=merchant_id,
                lead_rate=float(rate or 0.0),
            ))
        return items

    def _ledger_income(self, order: Order, now: float) -> None:
        for item in order.items:
            self._ledger(LedgerEntry(
                id=self.store.next_id("led"), at=now, kind=item.kind,
                direction="income",
                amount=item.quantity * item.unit_price,
                session_id=item.session_id, order_id=order.id,
                merchant_id=item.merchant_id,
                memo="购票确认收入",
            ))

    def _ledger(self, entry: LedgerEntry) -> None:
        self.store.post_ledger(entry)

    @staticmethod
    def _item_dict(item: OrderItem) -> dict[str, Any]:
        return {
            "kind": item.kind.value, "session_id": item.session_id,
            "quantity": item.quantity, "unit_price": item.unit_price,
            "merchant_id": item.merchant_id, "lead_rate": item.lead_rate,
        }

    def _snapshot_policy(self, order: Order, session_id: str) -> RulePolicy:
        snap = order.policy_snapshot.get(session_id)
        if snap is None:
            # 理论上不会发生：所有订单都在确认时冻结快照
            snap = self._session(session_id).policy.to_dict()
        return RulePolicy.from_dict(snap)

    def _migration_result(self, new_session_id: str,
                          extra: Optional[dict] = None) -> dict[str, Any]:
        new_session = self._session(new_session_id)
        result = {
            "new_session_id": new_session_id,
            "state": new_session.state.value,
            "capacity": self.store.capacity_view(new_session),
        }
        if extra:
            result.update(extra)
        return result

    # 快捷取值
    def _session(self, session_id: str) -> Session:
        session = self.store.sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"场次不存在: {session_id}")
        return session

    def _entrance(self, entrance_id: str) -> Entrance:
        entrance = self.store.entrances.get(entrance_id)
        if entrance is None:
            raise NotFoundError(f"入口不存在: {entrance_id}")
        return entrance

    def _route(self, route_id: str) -> Route:
        route = self.store.routes.get(route_id)
        if route is None:
            raise NotFoundError(f"游线不存在: {route_id}")
        return route

    def _hold(self, hold_id: str) -> Hold:
        hold = self.store.holds.get(hold_id)
        if hold is None:
            raise NotFoundError(f"锁位不存在: {hold_id}")
        return hold

    def _order(self, order_id: str) -> Order:
        order = self.store.orders.get(order_id)
        if order is None:
            raise NotFoundError(f"订单不存在: {order_id}")
        return order

    def _hold_from_replay(self, event, key: str) -> Hold:
        hold_id = event.data.get("hold_id")
        if hold_id and hold_id in self.store.holds:
            return self.store.holds[hold_id]
        # 极端情况：事件存在但内存丢失（不应发生），明确报错而非重复锁位
        raise StateError(f"幂等键 {key} 的原锁位已不可查")

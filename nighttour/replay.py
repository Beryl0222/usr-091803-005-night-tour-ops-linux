"""从事件日志重建运行状态。

服务重启（或离线设备补传前服务已重启）后，单靠幂等索引还不够，
内存中的资源/订单/计数也必须复原。重放器按事件顺序重建全部领域
对象；容量计数不依赖重放业务逻辑，直接折叠每个事件的
``capacity_effects``（其中已包含现场钳制后的真实增量），
因此结果与宕机前一致。
"""

from __future__ import annotations

from .models import (
    Alert,
    AlertLevel,
    AlertState,
    Area,
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
    Route,
    RulePolicy,
    Session,
    SessionState,
    TicketKind,
)


def replay_into(store) -> None:
    """按 seq 顺序把事件日志全部重放进空 Store（启动期单线程调用）。"""
    events = sorted(store.events.all(), key=lambda e: e.seq)
    for event in events:
        handler = _HANDLERS.get(event.type)
        if handler:
            handler(store, event.data)
    store._seq = max((e.seq for e in events), default=0)


# ---------------------------------------------------------------------------
# 资源
# ---------------------------------------------------------------------------


def _area_registered(store, d):
    store.areas[d["area_id"]] = Area(
        id=d["area_id"], name=d["name"], capacity=d["capacity"],
        kind=d.get("kind", "general"), backup_area_id=d.get("backup_area_id"))


def _entrance_registered(store, d):
    store.entrances[d["entrance_id"]] = Entrance(
        id=d["entrance_id"], name=d["name"], area_id=d["area_id"],
        quotas=dict(d.get("quotas") or {}))


def _entrance_quota_set(store, d):
    store.entrances[d["entrance_id"]].quotas[d["session_id"]] = d["quota"]
    session = store.sessions.get(d["session_id"])
    if session and d["entrance_id"] not in session.entrance_ids:
        session.entrance_ids.append(d["entrance_id"])


def _route_registered(store, d):
    store.routes[d["route_id"]] = Route(
        id=d["route_id"], name=d["name"], capacity=d["capacity"],
        area_ids=list(d.get("area_ids") or []))


def _facility_registered(store, d):
    facility = Facility(id=d["facility_id"], name=d["name"],
                        area_id=d["area_id"])
    store.facilities[d["facility_id"]] = facility
    area = store.areas.get(d["area_id"])
    if area and d["facility_id"] not in area.facility_ids:
        area.facility_ids.append(d["facility_id"])


def _facility_state_changed(store, d):
    facility = store.facilities.get(d["facility_id"])
    if facility:
        facility.state = FacilityState(d["new_state"])
        facility.detail = d.get("detail", "")


def _merchant_registered(store, d):
    store.merchants[d["merchant_id"]] = Merchant(
        id=d["merchant_id"], name=d["name"],
        default_lead_rate=d.get("default_lead_rate", 0.0))


# ---------------------------------------------------------------------------
# 场次
# ---------------------------------------------------------------------------


def _build_session(d) -> Session:
    return Session(
        id=d.get("id") or d["session_id"],
        title=d["title"], area_id=d["area_id"],
        kind=TicketKind(d["kind"]), start_at=d["start_at"], end_at=d["end_at"],
        state=SessionState(d["state"]) if d.get("state") else SessionState.SCHEDULED,
        policy=RulePolicy.from_dict(d["policy"]),
        entrance_ids=list(d.get("entrance_ids") or []),
        route_ids=list(d.get("route_ids") or []),
        migrated_from=d.get("migrated_from"),
        note=d.get("note", ""),
        reopen_conditions=list(d.get("reopen_conditions") or []),
    )


def _session_created(store, d):
    session = _build_session(d)
    if d.get("capacity") is not None:
        area = store.areas.get(session.area_id)
        if area and d["capacity"] != area.capacity:
            area.session_capacity[session.id] = d["capacity"]
    store.sessions[session.id] = session


def _session_suspended(store, d):
    session = store.sessions.get(d["session_id"])
    if session:
        session.state = SessionState.SUSPENDED
        session.suspended_reason = d.get("reason", "")


def _session_resumed(store, d):
    session = store.sessions.get(d["session_id"])
    if session:
        session.state = SessionState.SCHEDULED
        session.note = d.get("reason", "")


def _session_cancelled(store, d):
    session = store.sessions.get(d["session_id"])
    if session:
        session.state = SessionState.CANCELLED
        session.cancel_reason = d.get("reason", "")


def _session_ended(store, d):
    session = store.sessions.get(d["session_id"])
    if session:
        session.state = SessionState.CLOSED


def _session_migrated(store, d):
    old = store.sessions.get(d["session_id"])
    if old:
        old.state = SessionState.MIGRATED
        old.migrated_to = d["new_session_id"]
    if d["new_session_id"] not in store.sessions:
        new = _build_session({"id": d["new_session_id"], **d["new_session"]})
        store.sessions[new.id] = new
        for entrance_id in new.entrance_ids:
            entrance = store.entrances.get(entrance_id)
            if entrance is not None:
                entrance.quotas.setdefault(
                    new.id, store.areas[new.area_id].capacity)
    # 迁移订单：占用搬到新场次，状态视补偿而定
    target = d["new_session"]
    entrance_id = (target.get("entrance_ids") or [None])[0]
    route_id = (target.get("route_ids") or [None])[0]
    for item in d.get("migrated", []):
        order = store.orders.get(item["order_id"])
        if order is None:
            continue
        for line in order.items:
            line.session_id = d["new_session_id"]
        order.allocations = [{
            "session_id": d["new_session_id"],
            "entrance_id": entrance_id,
            "route_id": route_id,
            "quantity": item["quantity"],
        }]
        if item.get("compensation"):
            order.compensations.append(item["compensation"])
            order.status = OrderStatus.COMPENSATED


# ---------------------------------------------------------------------------
# 锁位与订单
# ---------------------------------------------------------------------------


def _hold_created(store, d):
    store.holds[d["hold_id"]] = Hold(
        id=d["hold_id"], session_id=d["session_id"],
        entrance_id=d["entrance_id"], route_id=d.get("route_id"),
        quantity=d["quantity"], channel=d.get("channel", ""),
        state=HoldState.HELD, expires_at=d.get("expires_at", 0.0))


def _hold_released(store, d):
    hold = store.holds.get(d["hold_id"])
    if hold:
        hold.state = HoldState.RELEASED


def _order_confirmed(store, d):
    hold = store.holds.get(d["hold_id"])
    items = [OrderItem(
        kind=TicketKind(i["kind"]), session_id=i["session_id"],
        quantity=i["quantity"], unit_price=i["unit_price"],
        merchant_id=i.get("merchant_id"), lead_rate=i.get("lead_rate", 0.0),
    ) for i in d["items"]]
    order = Order(
        id=d["order_id"], visitor_id=d["visitor_id"], items=items,
        status=OrderStatus.PAID, created_at=0,
        policy_snapshot={sid: dict(pol)
                         for sid, pol in d["policy_snapshot"].items()},
        paid_amount=d["paid_amount"], hold_ids=[d["hold_id"]],
        allocations=[{
            "session_id": d["session_id"],
            "entrance_id": d["entrance_id"],
            "route_id": d.get("route_id"),
            "quantity": d["quantity"],
        }],
    )
    store.orders[order.id] = order
    if hold:
        hold.state = HoldState.CONFIRMED
        hold.order_id = order.id


def _order_refunded(store, d):
    order = store.orders.get(d["order_id"])
    if order is None:
        return
    order.refunds.append({**d.get("result", {}), "reason": d.get("reason")})
    if d.get("result", {}).get("amount", 0) > 0:
        order.status = OrderStatus.REFUNDED
    # 未开场退款会释放全部维度占用（session 维度负增量即标志）
    released_session = any(
        e.get("scope") == "session" and e.get("occupied_delta", 0) < 0
        for e in d.get("capacity_effects", []))
    if released_session:
        order.allocations = []


def _order_rescheduled(store, d):
    order = store.orders.get(d["order_id"])
    if order is None:
        return
    for item in order.items:
        item.session_id = d["to_session_id"]
    order.allocations = [{
        "session_id": d["to_session_id"],
        "entrance_id": d["entrance_id"],
        "route_id": d.get("route_id"),
        "quantity": d["quantity"],
    }]
    order.reschedules.append({
        "from_session_id": d["from_session_id"],
        "to_session_id": d["to_session_id"],
        "entrance_id": d["entrance_id"], "route_id": d.get("route_id"),
        "fee": d.get("fee", 0),
    })


def _order_checked_out(store, d):
    order = store.orders.get(d["order_id"])
    if order:
        order.allocations = []


def _compensation_granted(store, d):
    order = store.orders.get(d["order_id"])
    if order is None:
        return
    order.compensations.append(d.get("compensation", {}))
    if order.status == OrderStatus.PAID:
        order.status = OrderStatus.COMPENSATED


# ---------------------------------------------------------------------------
# 安全
# ---------------------------------------------------------------------------


def _alert_raised(store, d):
    store.alerts[d["alert_id"]] = Alert(
        id=d["alert_id"], level=AlertLevel(d["level"]),
        scope=d["scope"], target_id=d["target_id"],
        title=d["title"], detail=d.get("detail", ""),
        created_at=d.get("at", 0.0), state=AlertState.OPEN,
        handler_id=d.get("handler_id", ""),
        recovery_conditions=list(d.get("recovery_conditions") or []))


def _alert_assigned(store, d):
    alert = store.alerts.get(d["alert_id"])
    if alert:
        alert.handler_id = d["handler_id"]


def _alert_conditions_updated(store, d):
    alert = store.alerts.get(d["alert_id"])
    if alert:
        alert.recovery_conditions = list(d.get("recovery_conditions") or [])


def _alert_recovered(store, d):
    alert = store.alerts.get(d["alert_id"])
    if alert:
        alert.state = AlertState.RECOVERED
        alert.recovered_reason = d.get("reason", "")
        alert.recovery_checks = list(d.get("checks") or [])
        alert.recovered_at = d.get("at", 0.0)
    for route_id in d.get("unblocked", []):
        route = store.routes.get(route_id)
        if route:
            route.blocked = False
            route.blocked_reason = ""


def _alert_closed(store, d):
    alert = store.alerts.get(d["alert_id"])
    if alert:
        alert.state = AlertState.CLOSED


def _route_restricted(store, d):
    route = store.routes.get(d["route_id"])
    if route:
        route.blocked = True
        route.blocked_reason = d.get("reason", "现场限流")
    alert = store.alerts.get(d.get("alert_id"))
    if alert:
        alert.actions.append({"type": "restrict_route",
                              "route_id": d["route_id"]})


def _visitors_transferred(store, d):
    for order_id in d.get("order_ids", []):
        order = store.orders.get(order_id)
        if order is None:
            continue
        for alloc in order.allocations:
            if alloc.get("route_id") == d["from_route_id"]:
                alloc["route_id"] = d.get("to_route_id")
    alert = store.alerts.get(d.get("alert_id"))
    if alert:
        alert.actions.append({"type": "transfer_visitors",
                              "quantity": d.get("quantity", 0)})


def _visitors_evacuated(store, d):
    for order_id in d.get("order_ids", []):
        order = store.orders.get(order_id)
        if order is not None:
            order.allocations = []
    alert = store.alerts.get(d.get("alert_id"))
    if alert:
        alert.actions.append({"type": "evacuate",
                              "quantity": d.get("quantity", 0)})


# ---------------------------------------------------------------------------
# 台账与容量折叠
# ---------------------------------------------------------------------------


def _ledger_posted(store, d):
    store.ledger.append(LedgerEntry(
        id=d["id"], at=d["at"], kind=TicketKind(d["kind"]),
        direction=d["direction"], amount=d["amount"],
        session_id=d["session_id"], order_id=d["order_id"],
        merchant_id=d.get("merchant_id"), memo=d.get("memo", "")))


def _fold_counters(store, d):
    for effect in d.get("capacity_effects", []):
        scope = effect["scope"]
        sid = effect.get("session_id", "")
        key = ((scope, effect["target_id"]) if scope == "session"
               else (scope, effect["target_id"], sid))
        bucket = store.counters.setdefault(key, {"held": 0, "occupied": 0})
        bucket["held"] += effect.get("held_delta", 0)
        bucket["occupied"] += effect.get("occupied_delta", 0)


_HANDLERS = {
    "area_registered": _area_registered,
    "entrance_registered": _entrance_registered,
    "entrance_quota_set": _entrance_quota_set,
    "route_registered": _route_registered,
    "facility_registered": _facility_registered,
    "facility_state_changed": _facility_state_changed,
    "merchant_registered": _merchant_registered,
    "session_created": _session_created,
    "session_suspended": _session_suspended,
    "session_resumed": _session_resumed,
    "session_cancelled": _session_cancelled,
    "session_ended": _session_ended,
    "session_migrated": _session_migrated,
    "hold_created": _hold_created,
    "hold_released": _hold_released,
    "order_confirmed": _order_confirmed,
    "order_refunded": _order_refunded,
    "order_rescheduled": _order_rescheduled,
    "order_checked_out": _order_checked_out,
    "compensation_granted": _compensation_granted,
    "alert_raised": _alert_raised,
    "alert_assigned": _alert_assigned,
    "alert_conditions_updated": _alert_conditions_updated,
    "alert_recovered": _alert_recovered,
    "alert_closed": _alert_closed,
    "route_restricted": _route_restricted,
    "visitors_transferred": _visitors_transferred,
    "visitors_evacuated": _visitors_evacuated,
    "ledger_posted": _ledger_posted,
}

# 所有处理器都先折叠容量，再重建对象
_ORIGINAL_HANDLERS = dict(_HANDLERS)


def _dispatch(store, d):  # pragma: no cover - 由下方绑定替代
    raise NotImplementedError


for _event_type, _handler in list(_HANDLERS.items()):
    def _make(inner):
        def wrapped(store, d):
            _fold_counters(store, d)
            inner(store, d)
        return wrapped
    _HANDLERS[_event_type] = _make(_handler)

# 容量事件可能没有专属处理器（如 field_action_reported 不含效果，可忽略）
for _extra in ("field_action_reported",):
    _HANDLERS[_extra] = lambda store, d: None

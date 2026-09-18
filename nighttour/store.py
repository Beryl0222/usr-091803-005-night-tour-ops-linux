"""线程安全的内存状态与容量计数。

所有变更都在单一可重入锁内完成，并遵循"先写事件日志、再改内存状态"
的顺序：事件落盘成功后状态才生效，崩溃时不会出现"有状态无记录"。
幂等键也落在事件上，离线重复上报时直接回放首次结果。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .events import EventLog
from .models import (
    Alert,
    Area,
    Entrance,
    Facility,
    Hold,
    Merchant,
    Order,
    Route,
    Session,
)


class CapacityError(Exception):
    """锁位/确认会突破容量上限。"""


class NotFoundError(Exception):
    """引用的领域对象不存在。"""


class StateError(Exception):
    """对象当前状态不允许该操作。"""


class Store:
    def __init__(self, event_log: Optional[EventLog] = None, clock: Callable[[], float] = None):
        self.lock = threading.RLock()
        self.clock = clock or time.time
        self.events = event_log or EventLog(clock=self.clock)

        self.areas: dict[str, Area] = {}
        self.entrances: dict[str, Entrance] = {}
        self.routes: dict[str, Route] = {}
        self.facilities: dict[str, Facility] = {}
        self.merchants: dict[str, Merchant] = {}
        self.sessions: dict[str, Session] = {}
        self.holds: dict[str, Hold] = {}
        self.orders: dict[str, Order] = {}
        self.alerts: dict[str, Alert] = {}
        self.ledger: list = []
        # (scope, target_id[, session_id]) -> {"held": n, "occupied": n}
        self.counters: dict[tuple, dict[str, int]] = {}
        self._seq = 0
        # 已重放的幂等键 -> 首次事件（事件日志之外的快速索引）
        self.idem_index: dict[str, str] = {}
        if self.events.all():
            # 从持久化日志重建完整运行态（资源/订单/告警/台账/容量计数）
            from .replay import replay_into

            replay_into(self)
        for event in self.events.all():
            if event.idempotency_key:
                self.idem_index[event.idempotency_key] = event.type
            self._seq = max(self._seq, event.seq)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def next_id(self, prefix: str) -> str:
        # 调用方需持锁
        self._seq += 1
        return f"{prefix}_{self._seq:06d}"

    def next_seq(self) -> int:
        # 调用方需持锁
        self._seq += 1
        return self._seq

    def now(self) -> float:
        return self.clock()

    def record(self, event_type: str, data: dict, actor: str = "system",
               idempotency_key: Optional[str] = None, at: Optional[float] = None):
        """先写日志。调用方必须持有锁。"""
        event = self.events.append(
            event_type, data, actor=actor, at=at, idempotency_key=idempotency_key
        )
        if idempotency_key:
            self.idem_index[idempotency_key] = event_type
        self._seq = max(self._seq, event.seq)
        return event

    def replayed(self, idempotency_key: Optional[str]):
        """若幂等键已执行过，返回首次事件，否则 None。"""
        if not idempotency_key:
            return None
        return self.events.has_idempotency_key(idempotency_key)

    def post_ledger(self, entry) -> None:
        """财务流水同时进入台账与事件日志（重启后可完整重建）。"""
        self.ledger.append(entry)
        self.events.append("ledger_posted", {
            "id": entry.id, "at": entry.at, "kind": entry.kind.value,
            "direction": entry.direction, "amount": entry.amount,
            "session_id": entry.session_id, "order_id": entry.order_id,
            "merchant_id": entry.merchant_id, "memo": entry.memo,
        }, actor="finance")

    # ------------------------------------------------------------------
    # 容量计数
    # ------------------------------------------------------------------

    def _bucket(self, key: tuple) -> dict[str, int]:
        return self.counters.setdefault(key, {"held": 0, "occupied": 0})

    def reading(self, key: tuple) -> dict[str, int]:
        bucket = self.counters.get(key)
        return dict(bucket) if bucket else {"held": 0, "occupied": 0}

    def _active_held(self, hold: Hold) -> bool:
        return hold.state.value == "held" and hold.expires_at > self.now()

    def capacity_keys(self, session: Session, entrance_id: str,
                      route_id: Optional[str]) -> list[tuple]:
        """一次锁位要同时占住的四个维度：入口配额 / 场次 / 区域 / 游线。"""
        keys = [
            ("entrance", entrance_id, session.id),
            ("session", session.id),
            ("area", session.area_id, session.id),
        ]
        if route_id:
            keys.append(("route", route_id, session.id))
        return keys

    def limits_for(self, session: Session, entrance_id: str,
                   route_id: Optional[str]) -> dict[tuple, int]:
        area = self.areas.get(session.area_id)
        entrance = self.entrances.get(entrance_id)
        limits: dict[tuple, int] = {}
        if entrance is None:
            raise NotFoundError(f"入口不存在: {entrance_id}")
        quota = entrance.quotas.get(session.id)
        if quota is None:
            raise StateError(f"入口 {entrance_id} 未参与场次 {session.id}")
        limits[("entrance", entrance_id, session.id)] = quota
        limits[("session", session.id)] = session_capacity(session, area)
        if area is not None:
            limits[("area", session.area_id, session.id)] = session_capacity(session, area)
        if route_id:
            route = self.routes.get(route_id)
            if route is None:
                raise NotFoundError(f"游线不存在: {route_id}")
            limits[("route", route_id, session.id)] = route.effective_capacity()
        return limits

    def assert_capacity(self, session: Session, entrance_id: str,
                        route_id: Optional[str], quantity: int) -> None:
        """校验新增 quantity 个锁位后任何维度都不超上限。"""
        limits = self.limits_for(session, entrance_id, route_id)
        for key, limit in limits.items():
            current = self._bucket(key)
            used = current["held"] + current["occupied"] + quantity
            if used > limit:
                scope = key[0]
                raise CapacityError(
                    f"容量不足: {scope} {key[1]} 需 {used}，上限 {limit}"
                )

    def apply_delta(self, keys: list[tuple], held: int = 0, occupied: int = 0,
                    clamp_scopes: Optional[set[str]] = None) -> list[tuple]:
        """变更计数，返回每个 key 实际施加的 (key, held_delta, occupied_delta)。

        clamp_scotes 内的维度（实际只有 "route"）允许把释放量钳制到桶内
        现有值：现场限流/转移会在订单不感知的情况下改变在途人数，按订单
        归还时可能超过桶内实际人数，差额来自现场动作，已在其事件中计过。
        """
        applied = []
        for key in keys:
            bucket = self._bucket(key)
            dh, do = held, occupied
            if clamp_scopes and key[0] in clamp_scopes:
                do = max(do, -bucket["occupied"])
                dh = max(dh, -bucket["held"])
            bucket["held"] += dh
            bucket["occupied"] += do
            assert bucket["held"] >= 0 and bucket["occupied"] >= 0, f"计数为负: {key}"
            applied.append((key, dh, do))
        return applied

    def capacity_view(self, session: Session) -> dict:
        """给出场次各维度的实时占用，供运营中心与审计使用。"""
        view = {"session": self._view(("session", session.id),
                                      session_capacity(session, self.areas.get(session.area_id)))}
        entrances = {}
        for entrance_id in session.entrance_ids:
            quota = self.entrances[entrance_id].quotas.get(session.id, 0)
            entrances[entrance_id] = self._view(
                ("entrance", entrance_id, session.id), quota
            )
        view["entrances"] = entrances
        area = self.areas.get(session.area_id)
        if area:
            view["area"] = self._view(
                ("area", session.area_id, session.id), session_capacity(session, area)
            )
        routes = {}
        for route_id in session.route_ids:
            route = self.routes[route_id]
            routes[route_id] = self._view(
                ("route", route_id, session.id), route.effective_capacity()
            )
        view["routes"] = routes
        return view

    def _view(self, key: tuple, limit: int) -> dict:
        reading = self.reading(key)
        used = reading["held"] + reading["occupied"]
        return {
            "used": used,
            "held": reading["held"],
            "occupied": reading["occupied"],
            "capacity": limit,
            "available": max(0, limit - used),
        }


def session_capacity(session: Session, area: Optional[Area]) -> int:
    """场次容量：优先取场次专属配置，否则取区域上限。"""
    if area is None:
        raise NotFoundError(f"区域不存在: {session.area_id}")
    return area.session_capacity.get(session.id, area.capacity)

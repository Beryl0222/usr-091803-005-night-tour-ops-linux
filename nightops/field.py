"""现场离线操作合并：限流与游客转移，按 action_id 幂等，不重复执行。"""

from __future__ import annotations

from .errors import Conflict, DomainError, Validation
from .models import FieldAction, LockState, ZoneStatus
from .store import require
from .capacity import effective_capacity, sweep_expired, zone_usage


class FieldService:
    def __init__(self, store):
        self.s = store

    def apply_batch(self, actions):
        """离线批量合并：逐条应用，单条失败不影响其他记录。"""
        if not isinstance(actions, list) or not actions:
            raise Validation("actions 必须是非空数组")
        with self.s.lock:
            sweep_expired(self.s)
            return [self._apply_one(action) for action in actions]

    def _apply_one(self, action):
        action_id = action.get("action_id") if isinstance(action, dict) else None
        if not action_id:
            return {"action_id": None, "status": "failed", "error": {"code": "validation", "message": "缺少 action_id"}}
        existing = self.s.field_actions.get(action_id)
        if existing is not None:
            # 离线重传：直接返回首次执行的结果，不重复执行
            return {"action_id": action_id, "status": "duplicate", "result": dict(existing.result)}
        try:
            result = self._execute(action)
        except DomainError as exc:
            # 失败的记录不落档，现场修正后可用同一 action_id 重试
            return {"action_id": action_id, "status": "failed", "error": {"code": exc.code, "message": exc.message}}
        record = FieldAction(
            action_id=action_id,
            staff_id=action.get("staff_id", ""),
            type=action.get("type", ""),
            payload=action.get("payload", {}),
            result=result,
            applied_at=round(self.s.clock(), 3),
        )
        self.s.field_actions[action_id] = record
        self.s.emit(
            "field_action",
            f"现场操作 {action_id}（{record.type}）已合并",
            action_id=action_id,
            type=record.type,
            staff_id=record.staff_id,
        )
        return {"action_id": action_id, "status": "applied", "result": result}

    def _execute(self, action):
        action_type = action.get("type")
        payload = action.get("payload") or {}
        if action_type == "flow_limit":
            return self._flow_limit(payload)
        if action_type == "transfer":
            return self._transfer(payload)
        raise Validation(f"未知的现场操作类型: {action_type}")

    def _flow_limit(self, payload):
        """现场限流：下调某区域某时段的有效容量；若低于已占用量则记为超员并提示风险。"""
        zone = require(self.s.zones, payload.get("zone_id"), "区域")
        date, slot = payload.get("date"), payload.get("slot")
        if not date or not slot:
            raise Validation("限流必须指定日期与时段")
        try:
            capacity = int(payload.get("capacity"))
        except (TypeError, ValueError):
            raise Validation("限流容量必须是整数")
        if capacity < 0:
            raise Validation("限流容量不能为负")
        key = (zone.zone_id, date, slot)
        self.s.capacity_overrides[key] = capacity
        held, confirmed = zone_usage(self.s, zone.zone_id, date, slot)
        used = held + confirmed
        effective = effective_capacity(self.s, zone, date, slot)
        overflow = max(0, used - effective)
        self.s.emit(
            "capacity_change",
            f"现场限流：{zone.name} {date} {slot} 容量下调至 {effective}",
            zone_id=zone.zone_id,
            date=date,
            slot=slot,
            delta=effective - zone.capacity,
            reason="field_flow_limit",
            override=capacity,
            used=used,
            overflow=overflow,
        )
        if overflow:
            self.s.emit(
                "risk_updated",
                f"区域 {zone.name} 限流后超员 {overflow} 人，需现场疏导",
                zone_id=zone.zone_id,
                date=date,
                slot=slot,
                overflow=overflow,
                status="open",
            )
        return {
            "zone_id": zone.zone_id,
            "date": date,
            "slot": slot,
            "effective_capacity": effective,
            "used": used,
            "overflow": overflow,
        }

    def _transfer(self, payload):
        """现场转移游客：把订单的已确认锁位迁到目标区域（应急处置，仍受区域容量约束）。"""
        to_zone = require(self.s.zones, payload.get("to_zone_id"), "目标区域")
        if to_zone.status is not ZoneStatus.OPEN:
            raise Conflict(f"目标区域 {to_zone.name} 未开放")
        order_ids = payload.get("order_ids") or []
        if not order_ids:
            raise Validation("转移必须指定 order_ids")
        orders = []
        for order_id in order_ids:
            order = require(self.s.orders, order_id, "订单")
            if order.status != "active":
                raise Conflict(f"订单 {order_id} 当前状态不可转移: {order.status}")
            orders.append(order)
        locks = []
        for order in orders:
            for lock_id in order.lock_ids:
                lock = self.s.locks[lock_id]
                if lock.state is LockState.CONFIRMED:
                    locks.append(lock)
        if not locks:
            raise Conflict("所选订单没有已确认的锁位可转移")
        # 按 (date, slot) 分组校验目标区域容量
        needed_by_slot = {}
        for lock in locks:
            needed_by_slot[(lock.date, lock.slot)] = needed_by_slot.get((lock.date, lock.slot), 0) + lock.count
        for (date, slot), needed in needed_by_slot.items():
            held, confirmed = zone_usage(self.s, to_zone.zone_id, date, slot)
            capacity = effective_capacity(self.s, to_zone, date, slot)
            if held + confirmed + needed > capacity:
                raise Conflict(
                    f"目标区域 {to_zone.name} 容量不足",
                    zone_id=to_zone.zone_id,
                    available=capacity - held - confirmed,
                    needed=needed,
                )
        moved = []
        for lock in locks:
            from_zone = lock.zone_id
            if from_zone == to_zone.zone_id:
                continue
            lock.zone_id = to_zone.zone_id
            moved.append(lock)
            self.s.emit(
                "capacity_change",
                f"现场转移：{lock.count} 人由 {from_zone} 转至 {to_zone.zone_id}",
                event_id=lock.event_id,
                zone_id=from_zone,
                date=lock.date,
                slot=lock.slot,
                delta=-lock.count,
                reason="field_transfer_out",
            )
            self.s.emit(
                "capacity_change",
                f"现场转移：{lock.count} 人进入 {to_zone.zone_id}",
                event_id=lock.event_id,
                zone_id=to_zone.zone_id,
                date=lock.date,
                slot=lock.slot,
                delta=lock.count,
                reason="field_transfer_in",
            )
        for order in orders:
            self.s.emit(
                "visitor_adjustment",
                f"订单 {order.order_id} 游客被现场转移至 {to_zone.zone_id}",
                event_id=order.event_id,
                order_id=order.order_id,
                action="transferred",
                to_zone_id=to_zone.zone_id,
            )
        return {
            "moved_orders": [o.order_id for o in orders],
            "visitors": sum(lock.count for lock in moved),
            "to_zone_id": to_zone.zone_id,
        }

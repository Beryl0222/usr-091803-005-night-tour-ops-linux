"""活动场次：多入口拆分、取消结算（退款/改期/补偿）、迁往备用区域、散场与回放。"""

from __future__ import annotations

from .errors import CapacityExceeded, Conflict, Validation
from .models import Allocation, Event, EventStatus, Lock, LockState, ZoneStatus
from .store import require
from .capacity import (
    allocation_usage,
    effective_capacity,
    sweep_expired,
    zone_usage,
)

# 取消责任方：scenic=景区责任（触发补偿），weather/other=不可抗力（只退不补）
RESPONSIBLE_KINDS = ("scenic", "weather", "other")


class EventService:
    def __init__(self, store):
        self.s = store

    # ---------- 排期 ----------

    def create_event(self, event_id, name, kind, date, slot, allocations, backup_zone_id=None):
        with self.s.lock:
            if event_id in self.s.events:
                raise Conflict(f"活动编号已存在: {event_id}")
            if not date or not slot:
                raise Validation("活动必须指定日期与时段")
            if not allocations:
                raise Validation("活动至少拆到一个入口/区域")
            parsed = []
            seen = set()
            for raw in allocations:
                zone_id = raw.get("zone_id")
                quota = raw.get("quota")
                zone = require(self.s.zones, zone_id, "区域")
                if zone_id in seen:
                    raise Validation(f"区域 {zone_id} 在活动中重复分配")
                seen.add(zone_id)
                try:
                    quota = int(quota)
                except (TypeError, ValueError):
                    raise Validation("配额必须是整数")
                if quota <= 0:
                    raise Validation("配额必须为正整数")
                if quota > zone.capacity:
                    raise Validation(f"区域 {zone.name} 容量 {zone.capacity} 小于配额 {quota}")
                parsed.append(Allocation(zone_id=zone_id, quota=quota))
            if backup_zone_id is not None:
                require(self.s.zones, backup_zone_id, "备用区域")
            event = Event(
                event_id=event_id,
                name=name,
                kind=kind,
                date=date,
                slot=slot,
                allocations=parsed,
                backup_zone_id=backup_zone_id,
            )
            self.s.events[event_id] = event
            self.s.emit(
                "event_created",
                f"活动 {name} 排期 {date} {slot}，拆分 {len(parsed)} 个入口",
                event_id=event_id,
                allocations=[a.to_dict() for a in parsed],
                backup_zone_id=backup_zone_id,
            )
            return event

    # ---------- 取消与结算 ----------

    def cancel_event(self, event_id, reason="", responsible="scenic", reschedule_event_id=None):
        """取消活动：按每笔订单购票时的政策快照计算退款/改期/补偿。"""
        with self.s.lock:
            sweep_expired(self.s)
            event = require(self.s.events, event_id, "活动")
            if event.status is EventStatus.CANCELLED:
                raise Conflict("活动已取消，请勿重复结算")
            if event.status is EventStatus.FINISHED:
                raise Conflict("活动已散场，不能取消")
            if responsible not in RESPONSIBLE_KINDS:
                raise Validation(f"责任方必须是 {RESPONSIBLE_KINDS}")
            target = None
            if reschedule_event_id is not None:
                target = require(self.s.events, reschedule_event_id, "改期目标活动")
                if target.event_id == event_id:
                    raise Validation("不能改期到活动自身")
                if target.status not in (EventStatus.SCHEDULED, EventStatus.RELOCATED):
                    raise Conflict(f"改期目标活动不可售: {target.status.value}")

            settlements = []
            active = [o for o in self.s.orders.values() if o.event_id == event_id and o.status == "active"]
            for order in sorted(active, key=lambda o: o.created_at):
                settlements.append(self._settle_order(order, reason, responsible, target))
            # 释放尚未确认的锁位
            for lock in self.s.locks.values():
                if lock.event_id == event_id and lock.state is LockState.HELD:
                    lock.state = LockState.RELEASED
                    self.s.emit(
                        "capacity_change",
                        f"活动取消，锁位 {lock.lock_id} 释放 {lock.count} 个名额",
                        event_id=event_id,
                        zone_id=lock.zone_id,
                        date=lock.date,
                        slot=lock.slot,
                        delta=-lock.count,
                        reason="event_cancelled",
                    )
            event.status = EventStatus.CANCELLED
            self.s.emit(
                "event_cancelled",
                f"活动 {event.name} 取消：{reason or '未说明'}",
                event_id=event_id,
                reason=reason,
                responsible=responsible,
                settled_orders=len(settlements),
            )
            return {"event_id": event_id, "status": event.status.value, "settlements": settlements}

    def _settle_order(self, order, reason, responsible, target):
        """单笔订单结算：优先按政策改期，否则退款并按责任方补偿。"""
        snapshot = order.policy_snapshot
        if target is not None and snapshot.get("allow_reschedule"):
            zone_id = self._find_reschedule_zone(target, order.visitors)
            if zone_id is not None:
                self._move_order_to_event(order, target, zone_id)
                self.s.emit(
                    "visitor_adjustment",
                    f"订单 {order.order_id} 改期至活动 {target.event_id}",
                    event_id=order.event_id,
                    order_id=order.order_id,
                    action="rescheduled",
                    to_event_id=target.event_id,
                    to_zone_id=zone_id,
                    visitors=order.visitors,
                )
                return {"order_id": order.order_id, "action": "rescheduled", "to_event_id": target.event_id}

        refund_cents = 0
        rate = float(snapshot.get("refund_rate", 0))
        for line in order.lines:
            line_refund = int(round(line.total_cents * rate))
            refund_cents += line_refund
            if line_refund:
                meta = {"merchant_id": line.merchant_id} if line.merchant_id else {}
                self.s.add_ledger(
                    account=line.kind,
                    kind="refund",
                    amount_cents=-line_refund,
                    reason=f"活动取消退款（政策 {snapshot.get('policy_id')}，比例 {rate}）",
                    event_id=order.event_id,
                    order_id=order.order_id,
                    meta=meta,
                )
        compensation_cents = 0
        if responsible == "scenic":
            compensation_cents = int(round(float(snapshot.get("compensation_per_ticket", 0)) * 100)) * order.visitors
            if compensation_cents:
                self.s.add_ledger(
                    account="compensation",
                    kind="compensation",
                    amount_cents=-compensation_cents,
                    reason=f"景区责任取消补偿（{order.visitors} 人）",
                    event_id=order.event_id,
                    order_id=order.order_id,
                )
        self._release_order_locks(order, reason="event_cancelled")
        order.status = "refunded"
        self.s.emit(
            "visitor_adjustment",
            f"订单 {order.order_id} 退款 {refund_cents / 100:.2f} 元，补偿 {compensation_cents / 100:.2f} 元",
            event_id=order.event_id,
            order_id=order.order_id,
            action="refunded",
            refund_cents=refund_cents,
            compensation_cents=compensation_cents,
            policy_id=snapshot.get("policy_id"),
        )
        return {
            "order_id": order.order_id,
            "action": "refunded",
            "refund": round(refund_cents / 100, 2),
            "compensation": round(compensation_cents / 100, 2),
        }

    def _find_reschedule_zone(self, target, visitors):
        """在目标活动的各入口中寻找能容纳这批游客的区域。"""
        for allocation in target.allocations:
            zone = self.s.zones[allocation.zone_id]
            held, confirmed = zone_usage(self.s, zone.zone_id, target.date, target.slot)
            capacity = effective_capacity(self.s, zone, target.date, target.slot)
            if held + confirmed + visitors > capacity:
                continue
            if allocation_usage(self.s, target.event_id, zone.zone_id) + visitors > allocation.quota:
                continue
            return zone.zone_id
        return None

    def _move_order_to_event(self, order, target, zone_id):
        self._release_order_locks(order, reason="rescheduled")
        lock = Lock(
            lock_id=self.s.next_id("lk"),
            key=f"reschedule:{order.order_id}:{self.s.clock()}",
            event_id=target.event_id,
            zone_id=zone_id,
            date=target.date,
            slot=target.slot,
            count=order.visitors,
            state=LockState.CONFIRMED,
            expires_at=self.s.clock(),
        )
        self.s.locks[lock.lock_id] = lock
        self.s.emit(
            "capacity_change",
            f"改期占用活动 {target.event_id} 区域 {zone_id} {order.visitors} 个名额",
            event_id=target.event_id,
            zone_id=zone_id,
            date=target.date,
            slot=target.slot,
            delta=order.visitors,
            reason="rescheduled_in",
        )
        order.event_id = target.event_id
        order.lock_ids = [lock.lock_id]

    def _release_order_locks(self, order, reason):
        for lock_id in order.lock_ids:
            lock = self.s.locks.get(lock_id)
            if lock is not None and lock.state in (LockState.HELD, LockState.CONFIRMED):
                lock.state = LockState.RELEASED
                self.s.emit(
                    "capacity_change",
                    f"订单 {order.order_id} 释放区域 {lock.zone_id} {lock.count} 个名额",
                    event_id=lock.event_id,
                    zone_id=lock.zone_id,
                    date=lock.date,
                    slot=lock.slot,
                    delta=-lock.count,
                    reason=reason,
                )

    # ---------- 迁往备用区域 ----------

    def relocate_event(self, event_id, to_zone_id=None, reason=""):
        """把活动整体迁往备用区域，已确认游客随活动转移，容量重新校验。"""
        with self.s.lock:
            sweep_expired(self.s)
            event = require(self.s.events, event_id, "活动")
            if event.status is EventStatus.FINISHED:
                raise Conflict("活动已散场，不能迁移")
            target_id = to_zone_id or event.backup_zone_id
            if not target_id:
                raise Validation("未指定备用区域，且活动未配置 backup_zone_id")
            zone = require(self.s.zones, target_id, "备用区域")
            if zone.status is not ZoneStatus.OPEN:
                raise Conflict(f"备用区域 {zone.name} 当前不可用: {zone.status.value}")
            if any(a.zone_id == target_id for a in event.allocations):
                raise Validation(f"活动已在区域 {target_id}")

            confirmed = [lk for lk in self.s.locks.values() if lk.event_id == event_id and lk.state is LockState.CONFIRMED]
            needed = sum(lk.count for lk in confirmed)
            held, used = zone_usage(self.s, target_id, event.date, event.slot)
            capacity = effective_capacity(self.s, zone, event.date, event.slot)
            if held + used + needed > capacity:
                raise CapacityExceeded(
                    f"备用区域 {zone.name} 容量不足",
                    zone_id=target_id,
                    available=capacity - held - used,
                    needed=needed,
                )
            from_zones = sorted({a.zone_id for a in event.allocations})
            moved_orders = sorted(
                {o.order_id for o in self.s.orders.values() if o.event_id == event_id and o.status == "active"}
            )
            for lock in confirmed:
                old_zone = lock.zone_id
                lock.zone_id = target_id
                self.s.emit(
                    "capacity_change",
                    f"迁移：{lock.count} 人由 {old_zone} 转至 {target_id}",
                    event_id=event_id,
                    zone_id=old_zone,
                    date=lock.date,
                    slot=lock.slot,
                    delta=-lock.count,
                    reason="relocate_out",
                )
                self.s.emit(
                    "capacity_change",
                    f"迁移：{lock.count} 人进入备用区域 {target_id}",
                    event_id=event_id,
                    zone_id=target_id,
                    date=lock.date,
                    slot=lock.slot,
                    delta=lock.count,
                    reason="relocate_in",
                )
            event.allocations = [Allocation(zone_id=target_id, quota=max(needed, 1))]
            event.status = EventStatus.RELOCATED
            for order_id in moved_orders:
                self.s.emit(
                    "visitor_adjustment",
                    f"订单 {order_id} 随活动迁往备用区域 {target_id}",
                    event_id=event_id,
                    order_id=order_id,
                    action="relocated",
                    to_zone_id=target_id,
                )
            self.s.emit(
                "event_relocated",
                f"活动 {event.name} 迁往备用区域 {zone.name}：{reason or '未说明'}",
                event_id=event_id,
                from_zones=from_zones,
                to_zone_id=target_id,
                visitors=needed,
                reason=reason,
            )
            return {
                "event_id": event_id,
                "status": event.status.value,
                "to_zone_id": target_id,
                "visitors_moved": needed,
                "orders_moved": moved_orders,
            }

    # ---------- 散场 ----------

    def finish_event(self, event_id):
        with self.s.lock:
            sweep_expired(self.s)
            event = require(self.s.events, event_id, "活动")
            if event.status is EventStatus.FINISHED:
                raise Conflict("活动已散场")
            for lock in self.s.locks.values():
                if lock.event_id == event_id and lock.state is LockState.HELD:
                    lock.state = LockState.EXPIRED
            event.status = EventStatus.FINISHED
            self.s.emit("event_finished", f"活动 {event.name} 散场", event_id=event_id)
            return event

    # ---------- 回放 ----------

    def replay_event(self, event_id):
        """还原一场活动：容量如何变化、哪些游客被调整、为何允许重新开放。"""
        with self.s.lock:
            event = require(self.s.events, event_id, "活动")
            entries = [e for e in self.s.log if e.event_id == event_id]
            zone_ids = {a.zone_id for a in event.allocations}
            for entry in entries:
                zone_id = entry.data.get("zone_id")
                if zone_id:
                    zone_ids.add(zone_id)
            related = list(entries)
            seen = {e.seq for e in entries}
            for entry in self.s.log:
                if entry.seq in seen:
                    continue
                if entry.kind == "reopen" and entry.data.get("zone_id") in zone_ids:
                    related.append(entry)
            related.sort(key=lambda e: e.seq)
            by_kind = lambda kind: [e.to_dict() for e in related if e.kind == kind]
            return {
                "event": event.to_dict(),
                "capacity_changes": by_kind("capacity_change"),
                "visitor_adjustments": by_kind("visitor_adjustment"),
                "reopen_records": by_kind("reopen"),
                "timeline": [e.to_dict() for e in related],
            }

    def event_view(self, event_id):
        with self.s.lock:
            sweep_expired(self.s)
            event = require(self.s.events, event_id, "活动")
            allocations = []
            for allocation in event.allocations:
                zone = self.s.zones[allocation.zone_id]
                held, confirmed = zone_usage(self.s, zone.zone_id, event.date, event.slot)
                allocations.append(
                    {
                        "zone_id": allocation.zone_id,
                        "quota": allocation.quota,
                        "used": allocation_usage(self.s, event_id, allocation.zone_id),
                        "zone_status": zone.status.value,
                        "zone_held": held,
                        "zone_confirmed": confirmed,
                    }
                )
            orders = [o for o in self.s.orders.values() if o.event_id == event_id]
            view = event.to_dict()
            view["allocation_usage"] = allocations
            view["orders"] = {"total": len(orders), "active": sum(1 for o in orders if o.status == "active")}
            return view

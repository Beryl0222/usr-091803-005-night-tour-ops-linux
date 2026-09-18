"""分时容量与锁位：并发锁位不能突破区域上限，也不能突破活动在各入口的配额。"""

from __future__ import annotations

from .errors import CapacityExceeded, Conflict, Validation
from .models import EventStatus, Lock, LockState, ZoneStatus
from .store import require


def sweep_expired(store):
    """惰性过期：把超时未确认的锁位置为 EXPIRED 并记录容量变化。"""
    now = store.clock()
    for lk in store.locks.values():
        if lk.state is LockState.HELD and lk.expires_at <= now:
            lk.state = LockState.EXPIRED
            store.emit(
                "capacity_change",
                f"锁位 {lk.lock_id} 超时释放 {lk.count} 个名额",
                event_id=lk.event_id,
                zone_id=lk.zone_id,
                date=lk.date,
                slot=lk.slot,
                delta=-lk.count,
                reason="lock_expired",
            )


def zone_usage(store, zone_id, date, slot):
    """某区域某时段 (持有中, 已确认) 的人数。"""
    held = confirmed = 0
    for lk in store.locks.values():
        if lk.zone_id == zone_id and lk.date == date and lk.slot == slot:
            if lk.state is LockState.HELD:
                held += lk.count
            elif lk.state is LockState.CONFIRMED:
                confirmed += lk.count
    return held, confirmed


def effective_capacity(store, zone, date, slot):
    """区域有效容量：未开放为 0；现场限流覆盖取较小值。"""
    if zone.status is not ZoneStatus.OPEN:
        return 0
    override = store.capacity_overrides.get((zone.zone_id, date, slot))
    if override is None:
        return zone.capacity
    return min(override, zone.capacity)


def allocation_usage(store, event_id, zone_id):
    """活动在某入口已占用的配额（持有 + 确认）。"""
    used = 0
    for lk in store.locks.values():
        if lk.event_id == event_id and lk.zone_id == zone_id and lk.state in (
            LockState.HELD,
            LockState.CONFIRMED,
        ):
            used += lk.count
    return used


class CapacityService:
    def __init__(self, store):
        self.s = store

    def place_lock(self, key, event_id, zone_id, count, ttl_seconds=300):
        """渠道锁位。同 key 重试返回原锁（幂等）；校验与写入在同一把锁内，并发安全。"""
        with self.s.lock:
            existing_id = self.s.lock_keys.get(key)
            if existing_id is not None:
                existing = self.s.locks[existing_id]
                if existing.state in (LockState.HELD, LockState.CONFIRMED):
                    return existing
            sweep_expired(self.s)
            if count <= 0:
                raise Validation("锁位数量必须为正整数")
            event = require(self.s.events, event_id, "活动")
            if event.status not in (EventStatus.SCHEDULED, EventStatus.RELOCATED):
                raise Conflict(f"活动当前状态不可锁位: {event.status.value}")
            zone = require(self.s.zones, zone_id, "区域")
            allocation = next((a for a in event.allocations if a.zone_id == zone_id), None)
            if allocation is None:
                raise Validation(f"活动 {event_id} 未在区域 {zone_id} 分配入口配额")

            held, confirmed = zone_usage(self.s, zone_id, event.date, event.slot)
            capacity = effective_capacity(self.s, zone, event.date, event.slot)
            if held + confirmed + count > capacity:
                raise CapacityExceeded(
                    f"区域 {zone.name} 容量不足",
                    zone_id=zone_id,
                    available=capacity - held - confirmed,
                    requested=count,
                )
            used_quota = allocation_usage(self.s, event_id, zone_id)
            if used_quota + count > allocation.quota:
                raise CapacityExceeded(
                    f"活动在该入口的配额不足",
                    zone_id=zone_id,
                    available=allocation.quota - used_quota,
                    requested=count,
                )
            lock = Lock(
                lock_id=self.s.next_id("lk"),
                key=key,
                event_id=event_id,
                zone_id=zone_id,
                date=event.date,
                slot=event.slot,
                count=count,
                state=LockState.HELD,
                expires_at=self.s.clock() + ttl_seconds,
            )
            self.s.locks[lock.lock_id] = lock
            self.s.lock_keys[key] = lock.lock_id
            self.s.emit(
                "capacity_change",
                f"锁位 {lock.lock_id} 占用 {count} 个名额",
                event_id=event_id,
                zone_id=zone_id,
                date=event.date,
                slot=event.slot,
                delta=count,
                reason="lock_placed",
            )
            return lock

    def release_lock(self, lock_id, reason="released"):
        with self.s.lock:
            lock = require(self.s.locks, lock_id, "锁位")
            if lock.state is not LockState.HELD:
                raise Conflict(f"锁位当前状态不可释放: {lock.state.value}")
            lock.state = LockState.RELEASED
            self.s.emit(
                "capacity_change",
                f"锁位 {lock_id} 释放 {lock.count} 个名额",
                event_id=lock.event_id,
                zone_id=lock.zone_id,
                date=lock.date,
                slot=lock.slot,
                delta=-lock.count,
                reason=reason,
            )
            return lock

    def capacity_view(self, zone_id, date, slot):
        with self.s.lock:
            sweep_expired(self.s)
            zone = require(self.s.zones, zone_id, "区域")
            held, confirmed = zone_usage(self.s, zone_id, date, slot)
            capacity = effective_capacity(self.s, zone, date, slot)
            return {
                "zone_id": zone_id,
                "date": date,
                "slot": slot,
                "zone_status": zone.status.value,
                "base_capacity": zone.capacity,
                "override": self.s.capacity_overrides.get((zone_id, date, slot)),
                "capacity": capacity,
                "held": held,
                "confirmed": confirmed,
                "available": capacity - held - confirmed,
            }

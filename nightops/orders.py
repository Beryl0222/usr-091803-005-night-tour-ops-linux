"""订单：确认锁位出票、按购票时政策留存快照、按收入类型记账。"""

from __future__ import annotations

from .errors import Conflict, Validation
from .models import EventStatus, LockState, Order, OrderLine
from .store import require
from .capacity import sweep_expired

LINE_KINDS = ("ticket", "show", "merchant")


def parse_line(raw):
    if not isinstance(raw, dict):
        raise Validation("订单明细必须是对象")
    kind = raw.get("kind")
    if kind not in LINE_KINDS:
        raise Validation(f"未知的收入类型: {kind}（可选 {LINE_KINDS}）")
    try:
        quantity = int(raw.get("quantity"))
    except (TypeError, ValueError):
        raise Validation("订单明细 quantity 必须是整数")
    if quantity <= 0:
        raise Validation("订单明细 quantity 必须为正整数")
    try:
        unit_price = float(raw.get("unit_price"))
    except (TypeError, ValueError):
        raise Validation("订单明细 unit_price 必须是数字")
    if unit_price < 0:
        raise Validation("订单明细 unit_price 不能为负")
    merchant_id = raw.get("merchant_id")
    if kind == "merchant" and not merchant_id:
        raise Validation("商户引流收入必须携带 merchant_id，否则无法分账")
    return OrderLine(
        kind=kind,
        quantity=quantity,
        unit_price_cents=int(round(unit_price * 100)),
        merchant_id=merchant_id,
    )


class OrderService:
    def __init__(self, store):
        self.s = store

    def create_order(self, order_id, event_id, visitor_id, lock_ids, policy_id, lines):
        """确认锁位并出票。同 order_id 重复提交返回原订单（幂等）。"""
        with self.s.lock:
            existing = self.s.orders.get(order_id)
            if existing is not None:
                return existing
            sweep_expired(self.s)
            event = require(self.s.events, event_id, "活动")
            if event.status not in (EventStatus.SCHEDULED, EventStatus.RELOCATED):
                raise Conflict(f"活动当前状态不可出票: {event.status.value}")
            policy = require(self.s.policies, policy_id, "退改政策")
            if not lock_ids:
                raise Validation("订单至少关联一个锁位")
            locks = []
            for lock_id in lock_ids:
                lock = require(self.s.locks, lock_id, "锁位")
                if lock.event_id != event_id:
                    raise Validation(f"锁位 {lock_id} 不属于活动 {event_id}")
                if lock.state is not LockState.HELD:
                    raise Conflict(f"锁位 {lock_id} 状态不可确认: {lock.state.value}")
                locks.append(lock)
            parsed = [parse_line(raw) for raw in lines]
            if not parsed:
                raise Validation("订单至少包含一条收入明细")

            for lock in locks:
                lock.state = LockState.CONFIRMED
                self.s.emit(
                    "capacity_change",
                    f"锁位 {lock.lock_id} 确认出票",
                    event_id=event_id,
                    zone_id=lock.zone_id,
                    date=lock.date,
                    slot=lock.slot,
                    delta=0,
                    reason="lock_confirmed",
                )
            order = Order(
                order_id=order_id,
                event_id=event_id,
                visitor_id=visitor_id,
                lines=parsed,
                lock_ids=[lock.lock_id for lock in locks],
                visitors=sum(lock.count for lock in locks),
                policy_snapshot=policy.snapshot(),
                status="active",
                created_at=round(self.s.clock(), 3),
            )
            self.s.orders[order_id] = order
            for line in parsed:
                meta = {"merchant_id": line.merchant_id} if line.merchant_id else {}
                self.s.add_ledger(
                    account=line.kind,
                    kind="sale",
                    amount_cents=line.total_cents,
                    reason="订单出票",
                    event_id=event_id,
                    order_id=order_id,
                    meta=meta,
                )
            self.s.emit(
                "order_confirmed",
                f"订单 {order_id} 出票 {order.visitors} 人",
                event_id=event_id,
                order_id=order_id,
                visitors=order.visitors,
                total_cents=order.total_cents,
            )
            return order

    def get_order(self, order_id):
        with self.s.lock:
            return require(self.s.orders, order_id, "订单")

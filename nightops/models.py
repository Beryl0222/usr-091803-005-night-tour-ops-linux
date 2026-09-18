"""景区夜游调度的领域对象：区域、路线、活动、锁位、订单、告警与台账。"""

from __future__ import annotations

import enum
from dataclasses import dataclass


def yuan(cents: int) -> float:
    """分转元，保留两位。"""
    return round(cents / 100, 2)


class ZoneKind(str, enum.Enum):
    ENTRANCE = "entrance"  # 入口
    STAGE = "stage"        # 舞台
    MARKET = "market"      # 市集
    AREA = "area"          # 游线途经区域
    BACKUP = "backup"      # 备用区域


class ZoneStatus(str, enum.Enum):
    OPEN = "open"
    MAINTENANCE = "maintenance"  # 设备检修
    CLOSED = "closed"            # 告警封闭


class EventKind(str, enum.Enum):
    TOUR = "tour"      # 夜游观光
    SHOW = "show"      # 实景演出
    MARKET = "market"  # 非遗市集


class EventStatus(str, enum.Enum):
    SCHEDULED = "scheduled"    # 已排期
    CANCELLED = "cancelled"    # 已取消
    RELOCATED = "relocated"    # 已迁往备用区域
    FINISHED = "finished"      # 已散场


class LockState(str, enum.Enum):
    HELD = "held"            # 持有中（渠道已锁位未支付）
    CONFIRMED = "confirmed"  # 已确认（出票）
    RELEASED = "released"    # 已释放
    EXPIRED = "expired"      # 超时未确认


@dataclass
class Zone:
    zone_id: str
    name: str
    kind: ZoneKind
    capacity: int  # 单时段容量上限
    status: ZoneStatus = ZoneStatus.OPEN

    def to_dict(self):
        return {
            "zone_id": self.zone_id,
            "name": self.name,
            "kind": self.kind.value,
            "capacity": self.capacity,
            "status": self.status.value,
        }


@dataclass
class Route:
    route_id: str
    name: str
    zone_ids: list  # 游线依次经过的区域

    def to_dict(self):
        return {"route_id": self.route_id, "name": self.name, "zone_ids": list(self.zone_ids)}


@dataclass
class Allocation:
    """活动在某一入口/区域的分时配额（一场活动可拆到多个入口）。"""

    zone_id: str
    quota: int

    def to_dict(self):
        return {"zone_id": self.zone_id, "quota": self.quota}


@dataclass
class Event:
    event_id: str
    name: str
    kind: EventKind
    date: str  # 例如 2026-09-18
    slot: str  # 例如 19:00-20:00
    allocations: list
    backup_zone_id: str | None = None
    status: EventStatus = EventStatus.SCHEDULED

    def to_dict(self):
        return {
            "event_id": self.event_id,
            "name": self.name,
            "kind": self.kind.value,
            "date": self.date,
            "slot": self.slot,
            "allocations": [a.to_dict() for a in self.allocations],
            "backup_zone_id": self.backup_zone_id,
            "status": self.status.value,
        }


@dataclass
class Lock:
    """渠道锁位：date/slot 冗余自活动，便于离线合并与审计。"""

    lock_id: str
    key: str  # 售票渠道的幂等键
    event_id: str
    zone_id: str
    date: str
    slot: str
    count: int
    state: LockState
    expires_at: float

    def to_dict(self):
        return {
            "lock_id": self.lock_id,
            "key": self.key,
            "event_id": self.event_id,
            "zone_id": self.zone_id,
            "date": self.date,
            "slot": self.slot,
            "count": self.count,
            "state": self.state.value,
            "expires_at": self.expires_at,
        }


@dataclass
class OrderLine:
    kind: str  # ticket=门票 show=演出 merchant=商户引流
    quantity: int
    unit_price_cents: int
    merchant_id: str | None = None

    @property
    def total_cents(self):
        return self.quantity * self.unit_price_cents

    def to_dict(self):
        data = {
            "kind": self.kind,
            "quantity": self.quantity,
            "unit_price": yuan(self.unit_price_cents),
            "total": yuan(self.total_cents),
        }
        if self.merchant_id:
            data["merchant_id"] = self.merchant_id
        return data


@dataclass
class Order:
    order_id: str
    event_id: str
    visitor_id: str
    lines: list
    lock_ids: list
    visitors: int
    policy_snapshot: dict  # 购票时的退改补偿规则快照，结算只认快照
    status: str            # active / refunded
    created_at: float

    @property
    def total_cents(self):
        return sum(line.total_cents for line in self.lines)

    def to_dict(self):
        return {
            "order_id": self.order_id,
            "event_id": self.event_id,
            "visitor_id": self.visitor_id,
            "lines": [line.to_dict() for line in self.lines],
            "lock_ids": list(self.lock_ids),
            "visitors": self.visitors,
            "policy_snapshot": dict(self.policy_snapshot),
            "status": self.status,
            "total": yuan(self.total_cents),
            "created_at": self.created_at,
        }


@dataclass
class RefundPolicy:
    policy_id: str
    name: str
    refund_rate: float              # 取消时的退款比例
    allow_reschedule: bool          # 是否允许改期
    compensation_per_ticket: float  # 景区责任取消时每张票的补偿（元）

    def snapshot(self):
        return {
            "policy_id": self.policy_id,
            "name": self.name,
            "refund_rate": self.refund_rate,
            "allow_reschedule": self.allow_reschedule,
            "compensation_per_ticket": self.compensation_per_ticket,
        }

    def to_dict(self):
        return self.snapshot()


@dataclass
class Alert:
    alert_id: str
    kind: str      # weather=气象预警 facility=设施检修
    severity: str  # low / medium / high
    zone_ids: list
    message: str
    status: str    # active / cleared
    created_at: float

    def to_dict(self):
        return {
            "alert_id": self.alert_id,
            "kind": self.kind,
            "severity": self.severity,
            "zone_ids": list(self.zone_ids),
            "message": self.message,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass
class RiskItem:
    """一条游线当前的风险档案：等级、处置人、恢复条件。"""

    route_id: str
    level: str                # low / medium / high
    assignee: str             # 处置人
    recovery_conditions: str  # 恢复条件
    status: str               # open / resolved
    updated_at: float

    def to_dict(self):
        return {
            "route_id": self.route_id,
            "level": self.level,
            "assignee": self.assignee,
            "recovery_conditions": self.recovery_conditions,
            "status": self.status,
            "updated_at": self.updated_at,
        }


@dataclass
class FieldAction:
    """现场人员离线操作的合并记录，action_id 为幂等键。"""

    action_id: str
    staff_id: str
    type: str  # flow_limit=限流 transfer=转移游客
    payload: dict
    result: dict
    applied_at: float

    def to_dict(self):
        return {
            "action_id": self.action_id,
            "staff_id": self.staff_id,
            "type": self.type,
            "payload": dict(self.payload),
            "result": dict(self.result),
            "applied_at": self.applied_at,
        }


@dataclass
class LedgerEntry:
    seq: int
    account: str       # ticket / show / merchant / compensation
    kind: str          # sale / refund / compensation
    amount_cents: int  # 正为收入，负为支出
    reason: str
    event_id: str | None
    order_id: str | None
    meta: dict
    ts: float

    def to_dict(self):
        return {
            "seq": self.seq,
            "account": self.account,
            "kind": self.kind,
            "amount": yuan(self.amount_cents),
            "reason": self.reason,
            "event_id": self.event_id,
            "order_id": self.order_id,
            "meta": dict(self.meta),
            "ts": self.ts,
        }


@dataclass
class LogEntry:
    """追加式运行记录：容量变化、游客调整、重新开放等都可据此还原。"""

    seq: int
    ts: float
    kind: str
    event_id: str | None
    summary: str
    data: dict

    def to_dict(self):
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "event_id": self.event_id,
            "summary": self.summary,
            "data": dict(self.data),
        }

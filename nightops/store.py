"""集中保存运行状态与审计日志，所有写操作都在同一把锁内完成。"""

from __future__ import annotations

import threading
import time

from .errors import NotFound
from .models import LedgerEntry, LogEntry


class Store:
    """内存仓储：业务状态 + 追加式运行记录（事件日志）+ 财务台账。"""

    def __init__(self, clock=None):
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.zones = {}              # zone_id -> Zone
        self.routes = {}             # route_id -> Route
        self.policies = {}           # policy_id -> RefundPolicy
        self.events = {}             # event_id -> Event
        self.locks = {}              # lock_id -> Lock
        self.lock_keys = {}          # 渠道幂等键 -> lock_id
        self.orders = {}             # order_id -> Order
        self.alerts = {}             # alert_id -> Alert
        self.risks = {}              # route_id -> RiskItem
        self.field_actions = {}      # action_id -> FieldAction（离线合并去重）
        self.capacity_overrides = {} # (zone_id, date, slot) -> 现场限流后的容量
        self.ledger = []             # LedgerEntry 列表
        self.log = []                # LogEntry 列表
        self._seq = 0
        self._id_seq = 0

    def next_id(self, prefix):
        self._id_seq += 1
        return f"{prefix}-{self._id_seq}"

    def emit(self, kind, summary, event_id=None, **data):
        """追加一条运行记录，调用方必须已持有 self.lock。"""
        self._seq += 1
        entry = LogEntry(
            seq=self._seq,
            ts=round(self.clock(), 3),
            kind=kind,
            event_id=event_id,
            summary=summary,
            data=data,
        )
        self.log.append(entry)
        return entry

    def add_ledger(self, account, kind, amount_cents, reason, event_id=None, order_id=None, meta=None):
        entry = LedgerEntry(
            seq=len(self.ledger) + 1,
            account=account,
            kind=kind,
            amount_cents=amount_cents,
            reason=reason,
            event_id=event_id,
            order_id=order_id,
            meta=meta or {},
            ts=round(self.clock(), 3),
        )
        self.ledger.append(entry)
        return entry


def require(mapping, key, what):
    """取对象或抛 NotFound。"""
    try:
        return mapping[key]
    except KeyError:
        raise NotFound(f"{what}不存在: {key}")

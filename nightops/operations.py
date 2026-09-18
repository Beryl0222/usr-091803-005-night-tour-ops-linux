"""门面：组合各领域服务，并维护区域/游线/退改政策等基础目录。"""

from __future__ import annotations

from .capacity import CapacityService
from .errors import Conflict, Validation
from .events import EventService
from .field import FieldService
from .finance import FinanceService
from .models import EventKind, RefundPolicy, Route, Zone, ZoneKind
from .orders import OrderService
from .safety import SafetyService
from .store import Store, require


class Operations:
    """运营中心的统一入口：所有业务操作共享同一份运行记录。"""

    def __init__(self, store=None):
        self.store = store or Store()
        self.capacity = CapacityService(self.store)
        self.orders = OrderService(self.store)
        self.events = EventService(self.store)
        self.safety = SafetyService(self.store)
        self.field = FieldService(self.store)
        self.finance = FinanceService(self.store)

    # ---------- 基础目录 ----------

    def create_zone(self, zone_id, name, kind, capacity):
        with self.store.lock:
            if zone_id in self.store.zones:
                raise Conflict(f"区域编号已存在: {zone_id}")
            try:
                kind = ZoneKind(kind)
            except ValueError:
                raise Validation(f"未知的区域类型: {kind}")
            try:
                capacity = int(capacity)
            except (TypeError, ValueError):
                raise Validation("区域容量必须是整数")
            if capacity <= 0:
                raise Validation("区域容量必须为正整数")
            zone = Zone(zone_id=zone_id, name=name, kind=kind, capacity=capacity)
            self.store.zones[zone_id] = zone
            self.store.emit("zone_status", f"登记区域 {name}（{kind.value}，容量 {capacity}）", zone_id=zone_id, status=zone.status.value)
            return zone

    def create_route(self, route_id, name, zone_ids):
        with self.store.lock:
            if route_id in self.store.routes:
                raise Conflict(f"游线编号已存在: {route_id}")
            if not zone_ids:
                raise Validation("游线至少经过一个区域")
            for zone_id in zone_ids:
                require(self.store.zones, zone_id, "区域")
            route = Route(route_id=route_id, name=name, zone_ids=list(zone_ids))
            self.store.routes[route_id] = route
            self.store.emit("route_created", f"登记游线 {name}", route_id=route_id, zone_ids=list(zone_ids))
            return route

    def upsert_policy(self, policy_id, name, refund_rate, allow_reschedule, compensation_per_ticket):
        """登记/更新退改政策。已售订单不受影响——结算只认购票时的快照。"""
        with self.store.lock:
            try:
                refund_rate = float(refund_rate)
                compensation_per_ticket = float(compensation_per_ticket)
            except (TypeError, ValueError):
                raise Validation("退款比例与补偿金额必须是数字")
            if not 0 <= refund_rate <= 1:
                raise Validation("退款比例必须在 0 到 1 之间")
            if compensation_per_ticket < 0:
                raise Validation("补偿金额不能为负")
            policy = RefundPolicy(
                policy_id=policy_id,
                name=name,
                refund_rate=refund_rate,
                allow_reschedule=bool(allow_reschedule),
                compensation_per_ticket=compensation_per_ticket,
            )
            self.store.policies[policy_id] = policy
            self.store.emit("policy_saved", f"登记退改政策 {name}", **policy.snapshot())
            return policy

    # ---------- 查询 ----------

    def list_zones(self):
        with self.store.lock:
            return [zone.to_dict() for zone in self.store.zones.values()]

    def list_routes(self):
        with self.store.lock:
            return [route.to_dict() for route in self.store.routes.values()]

    def list_events(self):
        with self.store.lock:
            return [event.to_dict() for event in self.store.events.values()]

    def create_event(self, event_id, name, kind, date, slot, allocations, backup_zone_id=None):
        try:
            kind = EventKind(kind)
        except ValueError:
            raise Validation(f"未知的活动类型: {kind}")
        return self.events.create_event(event_id, name, kind, date, slot, allocations, backup_zone_id)

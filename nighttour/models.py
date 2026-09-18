"""景区夜游运营中心的领域模型。

覆盖：区域/入口/游线/设施、活动场次与批次、锁位与订单、
规则快照、安全告警、商户与分账流水。所有可变状态都由
:class:`nighttour.store.Store` 加锁保护，模型本身保持朴素。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class CapacityScope(str, Enum):
    """容量计数维度。"""

    AREA = "area"          # 区域（观景/演出/市集等）
    ROUTE = "route"        # 游线（同时在途人数）
    SESSION = "session"    # 场次/演出批次
    ENTRANCE = "entrance"  # 入口分时配额


class FacilityState(str, Enum):
    NORMAL = "normal"
    MAINTENANCE = "maintenance"   # 计划检修
    FAULT = "fault"               # 突发故障
    SAFETY_CLOSED = "safety_closed"  # 安全原因关闭


class SessionState(str, Enum):
    SCHEDULED = "scheduled"    # 正常售票/运行
    SUSPENDED = "suspended"    # 暂停售票（预警等）
    CANCELLED = "cancelled"    # 已取消（原址不再开放）
    MIGRATED = "migrated"      # 已迁往备用区域
    CLOSED = "closed"          # 正常结束


class HoldState(str, Enum):
    HELD = "held"
    CONFIRMED = "confirmed"
    RELEASED = "released"
    CANCELLED = "cancelled"
    MIGRATED = "migrated"


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertState(str, Enum):
    OPEN = "open"                 # 未处置
    MITIGATED = "mitigated"       # 限流/转移等措施已执行
    RECOVERED = "recovered"       # 满足恢复条件，已复核重开
    CLOSED = "closed"             # 手工关闭（无需恢复）


class OrderStatus(str, Enum):
    HELD = "held"
    PAID = "paid"
    REFUNDED = "refunded"
    RESCHEDULED = "rescheduled"
    COMPENSATED = "compensated"   # 部分补偿后仍有效
    VOID = "void"


class TicketKind(str, Enum):
    """收入类别：财务必须分别核清。"""

    TICKET = "ticket"       # 门票
    SHOW = "show"           # 演出
    MERCHANT_LEAD = "merchant_lead"  # 商户引流


class Disposition(str, Enum):
    """游客被调整时的处置结果。"""

    UNCHANGED = "unchanged"
    REFUND = "refund"
    RESCHEDULE = "reschedule"
    MIGRATE = "migrate"
    PARTIAL_REFUND = "partial_refund"


# ---------------------------------------------------------------------------
# 基础结构
# ---------------------------------------------------------------------------


@dataclass
class Entrance:
    """入口。同一场次可在多个入口分摊配额。"""

    id: str
    name: str
    area_id: str
    active: bool = True
    # session_id -> 分时配额
    quotas: dict[str, int] = field(default_factory=dict)


@dataclass
class Area:
    """区域：观景区、舞台、市集、疏散集结点等。"""

    id: str
    name: str
    capacity: int
    kind: str = "general"            # general/stage/market/backup
    facility_ids: list[str] = field(default_factory=list)
    backup_area_id: Optional[str] = None
    active: bool = True
    # session_id -> 该场次在此区域的容量上限（缺省取 capacity）
    session_capacity: dict[str, int] = field(default_factory=dict)


@dataclass
class Route:
    """游线。占用按在途人数计，可被限流、转移与重开。"""

    id: str
    name: str
    capacity: int                    # 同时在途上限
    area_ids: list[str] = field(default_factory=list)
    active: bool = True
    blocked: bool = False            # 现场限流/安全阻断
    blocked_reason: str = ""

    def effective_capacity(self) -> int:
        """被限流/阻断的游线容量视为 0，新锁位一律拒绝。"""
        return 0 if self.blocked or not self.active else self.capacity


@dataclass
class Facility:
    """设备/设施：照明、舞台机械、闸机、广播等。"""

    id: str
    name: str
    area_id: str
    state: FacilityState = FacilityState.NORMAL
    detail: str = ""


@dataclass
class RulePolicy:
    """游客适用规则的快照内容（购票时锁定，后续改规则不影响老订单）。

    比例均为 0~1；补偿以代金券/二次入场券等非现金权益发放。
    """

    refund_cutoff_minutes: int            # 开演前可全额退款的截止分钟数
    late_refund_ratio: float              # 截止后、开场前的退款比例
    after_start_refund_ratio: float        # 开场后（园区原因）退款比例
    free_reschedule: bool                  # 是否允许免费改期
    reschedule_fee: int                    # 改期手续费（分）
    compensation_amount: int               # 取消/迁移时的补偿权益面额（分）
    compensation_on_migrate: bool          # 迁移到备用区是否补偿
    compensate_no_refund: bool = True      # 发补偿后是否保留订单（不强制退）

    def to_dict(self) -> dict[str, Any]:
        return {
            "refund_cutoff_minutes": self.refund_cutoff_minutes,
            "late_refund_ratio": self.late_refund_ratio,
            "after_start_refund_ratio": self.after_start_refund_ratio,
            "free_reschedule": self.free_reschedule,
            "reschedule_fee": self.reschedule_fee,
            "compensation_amount": self.compensation_amount,
            "compensation_on_migrate": self.compensation_on_migrate,
            "compensate_no_refund": self.compensate_no_refund,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RulePolicy":
        return cls(**data)


DEFAULT_POLICY = RulePolicy(
    refund_cutoff_minutes=120,
    late_refund_ratio=0.5,
    after_start_refund_ratio=0.8,
    free_reschedule=True,
    reschedule_fee=0,
    compensation_amount=2000,
    compensation_on_migrate=True,
)


@dataclass
class Session:
    """活动场次/演出批次。一场活动可拆多个批次、多个入口。"""

    id: str
    title: str
    area_id: str
    kind: TicketKind                  # ticket / show
    start_at: float
    end_at: float
    state: SessionState = SessionState.SCHEDULED
    policy: Optional[RulePolicy] = None
    entrance_ids: list[str] = field(default_factory=list)
    route_ids: list[str] = field(default_factory=list)
    # 迁移关系
    migrated_to: Optional[str] = None
    migrated_from: Optional[str] = None
    cancel_reason: str = ""
    suspended_reason: str = ""
    # 恢复条件（安全负责人填写，复核重开时逐条核验）
    reopen_conditions: list[str] = field(default_factory=list)
    resumed_from: Optional[str] = None   # 由哪个场次/批次恢复而来
    note: str = ""


@dataclass
class Hold:
    """锁位记录：渠道在支付前临时占容。"""

    id: str
    session_id: str
    entrance_id: str
    route_id: Optional[str]
    quantity: int
    channel: str
    state: HoldState = HoldState.HELD
    expires_at: float = 0.0
    order_id: Optional[str] = None
    created_at: float = 0.0


@dataclass
class OrderItem:
    """订单行：门票/演出/商户引流分别成行，便于分账。"""

    kind: TicketKind
    session_id: str
    quantity: int
    unit_price: int           # 单价（分）
    merchant_id: Optional[str] = None
    lead_rate: float = 0.0    # 商户引流：结算给商户的分成比例


@dataclass
class Order:
    id: str
    visitor_id: str
    items: list[OrderItem]
    status: OrderStatus
    created_at: float
    # 购票时规则快照：按 session_id 存，保证后续政策调整不溯及既往
    policy_snapshot: dict[str, dict[str, Any]]
    paid_amount: int = 0
    channel: str = ""
    hold_ids: list[str] = field(default_factory=list)
    # 当前占用落在哪些维度（改期/迁移会整体替换）
    allocations: list[dict[str, Any]] = field(default_factory=list)
    # 迁移/改期后指向的新订单
    successor_id: Optional[str] = None
    refunds: list[dict[str, Any]] = field(default_factory=list)
    compensations: list[dict[str, Any]] = field(default_factory=list)
    reschedules: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Alert:
    """安全告警：每条游线都要有风险级别、处置人和恢复条件。"""

    id: str
    level: AlertLevel
    scope: str                       # route / area / session / facility
    target_id: str
    title: str
    detail: str
    created_at: float
    state: AlertState = AlertState.OPEN
    handler_id: str = ""             # 处置人
    recovery_conditions: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)  # 限流/转移记录
    recovered_at: float = 0.0
    recovered_reason: str = ""
    recovery_checks: list[str] = field(default_factory=list)


@dataclass
class Merchant:
    id: str
    name: str
    default_lead_rate: float = 0.0


@dataclass
class LedgerEntry:
    """财务流水：门票/演出/商户引流三类严格分开。"""

    id: str
    at: float
    kind: TicketKind
    direction: str                   # income / refund / compensation / payout
    amount: int
    session_id: str
    order_id: str
    merchant_id: Optional[str] = None
    memo: str = ""


@dataclass
class CapacitySnapshot:
    """某一时刻的容量读数，进入审计时间线。"""

    scope: str
    target_id: str
    used: int
    capacity: int
    held: int

"""景区夜游运营中心：统一调度分时容量、游线、演出批次、设施与安全告警。"""

from .audit import AuditService
from .engine import Engine, RuleError
from .events import EventLog
from .finance import FinanceService
from .models import (
    AlertLevel,
    FacilityState,
    RulePolicy,
    SessionState,
    TicketKind,
)
from .safety import SafetyService
from .store import CapacityError, NotFoundError, StateError, Store


class OperationsCenter:
    """门面：组装存储、引擎与各领域服务，供 API 层直接调用。"""

    def __init__(self, event_log_path: str | None = None, clock=None):
        self.events = EventLog(path=event_log_path, clock=clock)
        self.store = Store(event_log=self.events, clock=clock)
        self.engine = Engine(self.store)
        self.safety = SafetyService(self.engine)
        self.finance = FinanceService(self.engine)
        self.audit = AuditService(self.engine)


__all__ = [
    "OperationsCenter", "Engine", "SafetyService", "FinanceService",
    "AuditService", "EventLog", "Store", "RulePolicy", "TicketKind",
    "AlertLevel", "FacilityState", "SessionState",
    "CapacityError", "NotFoundError", "StateError", "RuleError",
]

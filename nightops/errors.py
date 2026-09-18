"""领域错误：HTTP 层据此映射状态码。"""

from __future__ import annotations


class DomainError(Exception):
    """业务规则冲突的基类。"""

    status = 400
    code = "domain_error"

    def __init__(self, message, **detail):
        super().__init__(message)
        self.message = message
        self.detail = detail


class Validation(DomainError):
    """请求参数不合法。"""

    status = 400
    code = "validation"


class NotFound(DomainError):
    """引用的对象不存在。"""

    status = 404
    code = "not_found"


class Conflict(DomainError):
    """当前状态不允许该操作。"""

    status = 409
    code = "conflict"


class CapacityExceeded(Conflict):
    """锁位/迁移会突破区域容量或活动配额。"""

    code = "capacity_exceeded"

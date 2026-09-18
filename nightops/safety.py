"""安全：告警、路线风险（处置人/恢复条件）、设施状态联动与重新开放。"""

from __future__ import annotations

from .errors import Conflict, Validation
from .models import Alert, RiskItem, ZoneStatus
from .store import require

SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3}


class SafetyService:
    def __init__(self, store):
        self.s = store

    # ---------- 告警 ----------

    def raise_alert(self, alert_id, kind, severity, zone_ids, message):
        """气象预警/设施检修告警：自动把途经这些区域的游线标记为风险。"""
        with self.s.lock:
            if alert_id in self.s.alerts:
                raise Conflict(f"告警编号已存在: {alert_id}")
            if severity not in SEVERITY_ORDER:
                raise Validation(f"告警级别必须是 {tuple(SEVERITY_ORDER)}")
            if not zone_ids:
                raise Validation("告警至少关联一个区域")
            zones = [require(self.s.zones, zid, "区域") for zid in zone_ids]
            alert = Alert(
                alert_id=alert_id,
                kind=kind,
                severity=severity,
                zone_ids=[z.zone_id for z in zones],
                message=message,
                status="active",
                created_at=round(self.s.clock(), 3),
            )
            self.s.alerts[alert_id] = alert
            self.s.emit(
                "alert_raised",
                f"告警 {alert_id}（{kind}/{severity}）：{message}",
                zone_ids=alert.zone_ids,
                alert_id=alert_id,
            )
            self._refresh_route_risks(alert.zone_ids)
            return alert

    def clear_alert(self, alert_id):
        with self.s.lock:
            alert = require(self.s.alerts, alert_id, "告警")
            if alert.status != "active":
                raise Conflict(f"告警已解除: {alert_id}")
            alert.status = "cleared"
            self.s.emit("alert_cleared", f"告警 {alert_id} 已解除", zone_ids=alert.zone_ids, alert_id=alert_id)
            return alert

    def _refresh_route_risks(self, zone_ids):
        """按活动告警重算受影响游线的风险等级（只升不降，降级靠人工闭环）。"""
        affected = set(zone_ids)
        active = [a for a in self.s.alerts.values() if a.status == "active"]
        for route in self.s.routes.values():
            if not affected.intersection(route.zone_ids):
                continue
            route_alerts = [a for a in active if set(a.zone_ids).intersection(route.zone_ids)]
            if not route_alerts:
                continue
            level = max(route_alerts, key=lambda a: SEVERITY_ORDER[a.severity]).severity
            risk = self.s.risks.get(route.route_id)
            if risk is None or risk.status == "resolved":
                risk = RiskItem(
                    route_id=route.route_id,
                    level=level,
                    assignee=risk.assignee if risk else "",
                    recovery_conditions=risk.recovery_conditions if risk else "",
                    status="open",
                    updated_at=round(self.s.clock(), 3),
                )
            elif SEVERITY_ORDER[level] > SEVERITY_ORDER[risk.level]:
                risk.level = level
                risk.updated_at = round(self.s.clock(), 3)
            self.s.risks[route.route_id] = risk
            self.s.emit(
                "risk_updated",
                f"游线 {route.name} 风险等级 {risk.level}",
                route_id=route.route_id,
                level=risk.level,
                status=risk.status,
            )

    # ---------- 路线风险 ----------

    def set_risk_handler(self, route_id, assignee, recovery_conditions):
        """安全负责人登记处置人与恢复条件。"""
        with self.s.lock:
            route = require(self.s.routes, route_id, "游线")
            risk = self.s.risks.get(route_id)
            if risk is None:
                risk = RiskItem(
                    route_id=route_id,
                    level="low",
                    assignee="",
                    recovery_conditions="",
                    status="open",
                    updated_at=round(self.s.clock(), 3),
                )
                self.s.risks[route_id] = risk
            if assignee:
                risk.assignee = assignee
            if recovery_conditions:
                risk.recovery_conditions = recovery_conditions
            risk.updated_at = round(self.s.clock(), 3)
            self.s.emit(
                "risk_updated",
                f"游线 {route.name} 登记处置人 {risk.assignee}",
                route_id=route_id,
                assignee=risk.assignee,
                recovery_conditions=risk.recovery_conditions,
                status=risk.status,
            )
            return risk

    def resolve_risk(self, route_id):
        """确认恢复条件已满足：要求该游线途经区域没有未解除的告警。"""
        with self.s.lock:
            route = require(self.s.routes, route_id, "游线")
            risk = require(self.s.risks, route_id, "风险记录")
            blocking = [
                a.alert_id
                for a in self.s.alerts.values()
                if a.status == "active" and set(a.zone_ids).intersection(route.zone_ids)
            ]
            if blocking:
                raise Conflict("游线仍有未解除的告警", alerts=blocking)
            risk.status = "resolved"
            risk.updated_at = round(self.s.clock(), 3)
            self.s.emit(
                "risk_updated",
                f"游线 {route.name} 风险闭环",
                route_id=route_id,
                status="resolved",
            )
            return risk

    def route_risk_view(self, route_id=None):
        """安全负责人视图：每条路线的风险、处置人、恢复条件。"""
        with self.s.lock:
            routes = [require(self.s.routes, route_id, "游线")] if route_id else list(self.s.routes.values())
            view = []
            for route in routes:
                risk = self.s.risks.get(route.route_id)
                active_alerts = [
                    a.to_dict()
                    for a in self.s.alerts.values()
                    if a.status == "active" and set(a.zone_ids).intersection(route.zone_ids)
                ]
                view.append(
                    {
                        "route_id": route.route_id,
                        "name": route.name,
                        "level": risk.level if risk else "low",
                        "status": risk.status if risk else "none",
                        "assignee": risk.assignee if risk else "",
                        "recovery_conditions": risk.recovery_conditions if risk else "",
                        "active_alerts": active_alerts,
                        "zones": [
                            {"zone_id": zid, "status": self.s.zones[zid].status.value}
                            for zid in route.zone_ids
                        ],
                    }
                )
            return view

    # ---------- 设施状态与重新开放 ----------

    def set_zone_status(self, zone_id, status):
        """设备检修/封闭：状态变更会自动产生设施告警并联动游线风险。"""
        with self.s.lock:
            zone = require(self.s.zones, zone_id, "区域")
            try:
                target = ZoneStatus(status)
            except ValueError:
                raise Validation(f"未知的区域状态: {status}")
            if target is ZoneStatus.OPEN:
                raise Validation("重新开放必须走 reopen 接口，说明审批人与理由")
            if zone.status is target:
                raise Conflict(f"区域已处于状态: {status}")
            zone.status = target
            self.s.emit(
                "zone_status",
                f"区域 {zone.name} 状态变更为 {status}",
                zone_id=zone_id,
                status=status,
            )
            if target in (ZoneStatus.MAINTENANCE, ZoneStatus.CLOSED):
                severity = "medium" if target is ZoneStatus.MAINTENANCE else "high"
                self.raise_alert(
                    alert_id=self.s.next_id("alert"),
                    kind="facility",
                    severity=severity,
                    zone_ids=[zone_id],
                    message=f"设施状态变更：{zone.name} → {status}",
                )
            return zone

    def reopen_zone(self, zone_id, approver, reason):
        """重新开放：要求告警解除且相关游线风险闭环，记录审批人与理由。"""
        with self.s.lock:
            zone = require(self.s.zones, zone_id, "区域")
            if zone.status is ZoneStatus.OPEN:
                raise Conflict("区域已开放")
            if not approver or not reason:
                raise Validation("重新开放必须说明审批人与理由")
            blocking_alerts = [
                a.alert_id
                for a in self.s.alerts.values()
                if a.status == "active" and zone_id in a.zone_ids
            ]
            if blocking_alerts:
                raise Conflict("存在未解除的告警，不能重新开放", alerts=blocking_alerts)
            blocking_risks = [
                risk.route_id
                for route in self.s.routes.values()
                if zone_id in route.zone_ids
                for risk in [self.s.risks.get(route.route_id)]
                if risk is not None and risk.status != "resolved"
            ]
            if blocking_risks:
                raise Conflict("相关游线风险未闭环，恢复条件未确认", routes=blocking_risks)
            zone.status = ZoneStatus.OPEN
            self.s.emit(
                "reopen",
                f"区域 {zone.name} 重新开放",
                zone_id=zone_id,
                approver=approver,
                reason=reason,
            )
            return zone

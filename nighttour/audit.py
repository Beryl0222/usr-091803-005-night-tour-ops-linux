"""场次审计还原。

任一场次结束后，调用 :meth:`AuditService.session_timeline` 即可回答三件事：
1. 容量如何变化——重放每个事件的 capacity_effects，得到逐时点读数；
2. 哪些游客被调整——退款、改期、迁移、补偿、现场转移/疏散全部归并；
3. 最终为何允许重新开放——恢复事件所核验的条件清单、证据与原因。

全部结论都来自事件日志，可离线重放、可第三方核对，不依赖内存当前态。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


# 对容量读数有意义的事件
_CAPACITY_EVENTS = {
    "hold_created", "hold_released", "order_confirmed",
    "order_refunded", "order_rescheduled", "order_checked_out",
    "session_cancelled", "session_migrated", "session_resumed",
    "session_ended", "visitors_transferred", "visitors_evacuated",
}

_VISITOR_EVENTS = {
    "order_confirmed", "order_refunded", "order_rescheduled",
    "session_cancelled", "session_migrated", "compensation_granted",
    "visitors_transferred", "visitors_evacuated",
}


class AuditService:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def session_timeline(self, session_id: str,
                         include_migrated: bool = True) -> dict[str, Any]:
        with self.store.lock:
            ids = {session_id}
            session = self.store.sessions.get(session_id)
            route_ids: set[str] = set(session.route_ids) if session else set()
            if include_migrated and session is not None:
                if session.migrated_to:
                    ids.add(session.migrated_to)
                    migrated = self.store.sessions.get(session.migrated_to)
                    if migrated:
                        route_ids.update(migrated.route_ids)
                if session.migrated_from:
                    ids.add(session.migrated_from)

            events = [e for e in self.store.events.all()
                      if self._event_matches_session(e, session_id, route_ids)
                      or self._event_touches(e, ids)]
            events.sort(key=lambda e: (e.at, e.seq))

            capacity_track = self._replay_capacity(events)
            adjustments = self._collect_adjustments(events)
            reopen = self._reopen_story(events, ids)

            related = sorted(
                sid for sid in ids
                if sid in self.store.sessions and sid != session_id
            )
            final_session = (
                self.store.sessions.get(session.migrated_to)
                if session is not None and session.migrated_to else session
            )
            return {
                "session_id": session_id,
                "related_session_ids": related,
                "final_state": final_session.state.value if final_session else None,
                "events": [self._event_brief(e) for e in events],
                "capacity": capacity_track,
                "visitor_adjustments": adjustments,
                "reopen": reopen,
            }

    # ------------------------------------------------------------------
    # 容量重放
    # ------------------------------------------------------------------

    def _replay_capacity(self, events) -> dict[str, Any]:
        held: dict[tuple, int] = defaultdict(int)
        occupied: dict[tuple, int] = defaultdict(int)
        checkpoints = []
        for event in events:
            effects = event.data.get("capacity_effects") or []
            changed = False
            for effect in effects:
                key = (effect["scope"], effect["target_id"],
                       effect.get("session_id", ""))
                held[key] += effect.get("held_delta", 0)
                occupied[key] += effect.get("occupied_delta", 0)
                changed = True
            if changed and event.type in _CAPACITY_EVENTS:
                checkpoints.append({
                    "seq": event.seq, "at": event.at,
                    "event": event.type, "actor": event.actor,
                    "readings": [
                        {"scope": scope, "target_id": target,
                         "session_id": sid,
                         "held": held[(scope, target, sid)],
                         "occupied": occupied[(scope, target, sid)]}
                        for (scope, target, sid) in sorted(held.keys() | occupied.keys())
                        if held[(scope, target, sid)] or occupied[(scope, target, sid)]
                    ],
                })
        final = [
            {"scope": scope, "target_id": target, "session_id": sid,
             "held": held[(scope, target, sid)],
             "occupied": occupied[(scope, target, sid)]}
            for (scope, target, sid) in sorted(held.keys() | occupied.keys())
        ]
        return {"checkpoints": checkpoints, "final_replayed": final}

    # ------------------------------------------------------------------
    # 游客调整归并
    # ------------------------------------------------------------------

    def _collect_adjustments(self, events) -> dict[str, Any]:
        per_order: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"events": [], "refund_total": 0, "compensation_total": 0,
                     "reschedules": [], "dispositions": []}
        )
        field_counts = {"transferred": 0, "evacuated": 0}
        # 迁移事件自带退款/迁移明细，其中订单的 order_refunded 事件不再重复计
        migration_refunded = {p["order_id"] for ev in events
                              if ev.type == "session_migrated"
                              for p in ev.data.get("refunded", [])}
        for event in events:
            if event.type not in _VISITOR_EVENTS:
                continue
            data = event.data
            if event.type == "order_refunded":
                if data["order_id"] in migration_refunded:
                    continue
                row = per_order[data["order_id"]]
                result = data.get("result", {})
                row["refund_total"] += result.get("amount", 0)
                row["dispositions"].append(result.get("disposition"))
                row["events"].append(self._event_brief(event))
            elif event.type == "order_rescheduled":
                row = per_order[data["order_id"]]
                row["reschedules"].append({
                    "from": data.get("from_session_id"),
                    "to": data.get("to_session_id"),
                    "entrance_id": data.get("entrance_id"),
                    "route_id": data.get("route_id"),
                })
                row["events"].append(self._event_brief(event))
            elif event.type == "compensation_granted":
                row = per_order[data["order_id"]]
                comp = data.get("compensation", {})
                row["compensation_total"] += comp.get("amount", 0)
                row["events"].append(self._event_brief(event))
            elif event.type == "session_migrated":
                for item in data.get("migrated", []):
                    row = per_order[item["order_id"]]
                    row["dispositions"].append("migrate")
                    if item.get("compensation"):
                        row["compensation_total"] += item["compensation"]["amount"]
                    row["events"].append(self._event_brief(event))
                for item in data.get("refunded", []):
                    row = per_order[item["order_id"]]
                    row["refund_total"] += item.get("amount", 0)
                    row["dispositions"].append(item.get("disposition"))
            elif event.type == "visitors_transferred":
                field_counts["transferred"] += data.get("quantity", 0)
            elif event.type == "visitors_evacuated":
                field_counts["evacuated"] += data.get("quantity", 0)
            elif event.type == "order_confirmed":
                per_order[data["order_id"]]["events"].append(
                    self._event_brief(event))
        orders = []
        for order_id, row in sorted(per_order.items()):
            orders.append({"order_id": order_id, **{
                k: v for k, v in row.items() if k != "events"},
                "event_count": len(row["events"])})
        totals = {
            "affected_orders": len(per_order),
            "refund_total": sum(r["refund_total"] for r in per_order.values()),
            "compensation_total": sum(
                r["compensation_total"] for r in per_order.values()),
            **field_counts,
        }
        return {"orders": orders, "totals": totals}

    # ------------------------------------------------------------------
    # 重开原因
    # ------------------------------------------------------------------

    def _reopen_story(self, events, session_ids: set[str]) -> dict[str, Any]:
        suspends, resumes, recoveries = [], [], []
        for event in events:
            if event.type == "session_suspended":
                suspends.append({
                    "at": event.at, "reason": event.data.get("reason"),
                    "required_conditions": event.data.get("reopen_conditions", []),
                })
            elif event.type == "session_resumed":
                resumes.append({
                    "at": event.at, "reason": event.data.get("reason"),
                    "verified_checks": event.data.get("recovery_checks", []),
                    "capacity_after": event.data.get("capacity_after"),
                    "decision": "全部恢复条件已核验且相关设施正常，允许重新开放",
                })
            elif event.type == "alert_recovered":
                recoveries.append({
                    "at": event.at,
                    "target": f"{event.data.get('scope')}:{event.data.get('target_id')}",
                    "reason": event.data.get("reason"),
                    "verified_checks": event.data.get("checks", []),
                    "unblocked": event.data.get("unblocked", []),
                    "decision": "恢复条件逐条核验通过，解除限流/阻断",
                })
        allowed = bool(resumes or recoveries)
        return {
            "suspensions": suspends,
            "resumptions": resumes,
            "alert_recoveries": recoveries,
            "reopened": allowed,
            "why": (resumes[-1]["reason"] if resumes else
                    (recoveries[-1]["reason"] if recoveries else "")),
        }

    # ------------------------------------------------------------------

    @staticmethod
    def _event_touches(event, session_ids: set[str]) -> bool:
        data = event.data
        if data.get("session_id") in session_ids:
            return True
        if data.get("from_session_id") in session_ids:
            return True
        new_id = data.get("new_session_id")
        if new_id and new_id in session_ids:
            return True
        if event.type == "alert_recovered":
            return data.get("target_id") in session_ids
        return False

    def _event_matches_session(self, event, session_id: str,
                               route_ids: set[str]) -> bool:
        data = event.data
        if self._event_touches(event, {session_id}):
            return True
        # 与场次关联游线上的安全事件也属于该场次时间线
        if data.get("route_id") in route_ids:
            return True
        if data.get("from_route_id") in route_ids:
            return True
        if event.type in ("alert_raised", "alert_recovered",
                          "route_restricted"):
            if data.get("scope") == "route" and data.get("target_id") in route_ids:
                return True
        return False

    @staticmethod
    def _event_brief(event) -> dict[str, Any]:
        return {
            "seq": event.seq, "at": event.at, "type": event.type,
            "actor": event.actor,
            "idempotency_key": event.idempotency_key,
            "data": event.data,
        }

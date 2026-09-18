"""财务核算。

门票、演出、商户引流三类收入严格分开：每条流水都带 TicketKind，
汇总时逐类给出收入、退款、补偿与净额；商户引流再按订单行上的
lead_rate 给出应结商户金额，保证"商户拿得到可靠的客流分账"。
"""

from __future__ import annotations

from typing import Any, Optional

from .models import TicketKind


class FinanceService:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def entries(self, kind: Optional[str] = None,
                merchant_id: Optional[str] = None,
                session_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self.store.lock:
            out = []
            for entry in self.store.ledger:
                if kind and entry.kind.value != kind:
                    continue
                if merchant_id and entry.merchant_id != merchant_id:
                    continue
                if session_id and entry.session_id != session_id:
                    continue
                out.append(self._entry_dict(entry))
            return out

    def summary(self, session_id: Optional[str] = None) -> dict[str, Any]:
        """按收入类别核清：收入 / 退款 / 补偿 / 手续费 / 净额。"""
        with self.store.lock:
            buckets = {kind.value: {
                "income": 0, "refund": 0, "compensation": 0,
                "reschedule_fee": 0, "net": 0, "count": 0,
            } for kind in TicketKind}
            for entry in self.store.ledger:
                if session_id and entry.session_id != session_id:
                    continue
                bucket = buckets[entry.kind.value]
                if entry.direction in bucket:
                    bucket[entry.direction] += entry.amount
                bucket["count"] += 1
            for bucket in buckets.values():
                bucket["net"] = (bucket["income"] + bucket["reschedule_fee"]
                                 - bucket["refund"] - bucket["compensation"])
            total = {k: sum(b[k] for b in buckets.values())
                     for k in ("income", "refund", "compensation",
                               "reschedule_fee", "net", "count")}
            return {
                "by_kind": buckets,
                "total": total,
                "currency_unit": "分",
                "session_id": session_id,
            }

    def merchant_settlements(self, session_id: Optional[str] = None) -> list[dict[str, Any]]:
        """商户引流分账：逐商户列出引流单量、引流收入与应结金额。

        应结金额直接按订单行购票时锁定的 lead_rate 计算（事后调费率不
        溯及既往），退款按流水冲减，避免多结。
        """
        with self.store.lock:
            merchants: dict[str, dict[str, Any]] = {}
            for order in self.store.orders.values():
                for item in order.items:
                    if item.kind != TicketKind.MERCHANT_LEAD:
                        continue
                    if session_id and item.session_id != session_id:
                        continue
                    if not item.merchant_id:
                        continue
                    row = merchants.setdefault(item.merchant_id, {
                        "merchant_id": item.merchant_id,
                        "merchant_name": (
                            self.store.merchants[item.merchant_id].name
                            if item.merchant_id in self.store.merchants else ""),
                        "lead_quantity": 0, "gross_income": 0,
                        "gross_payable": 0, "refund": 0, "paid_out": 0,
                        "orders": set(),
                    })
                    gross = item.quantity * item.unit_price
                    row["lead_quantity"] += item.quantity
                    row["gross_income"] += gross
                    row["gross_payable"] += int(gross * item.lead_rate)
                    row["orders"].add(order.id)
            # 退款冲减：按被退金额占该行收入的近似比例难以精确，因此
            # 退款流水的分账冲减按该订单行的 lead_rate 计算（流水 memo
            # 里保留订单，便于逐笔核对）。
            refund_rate_by_order = {}
            for order in self.store.orders.values():
                rates = {i.lead_rate for i in order.items
                         if i.kind == TicketKind.MERCHANT_LEAD}
                if len(rates) == 1:
                    refund_rate_by_order[order.id] = rates.pop()
            for entry in self.store.ledger:
                if entry.kind != TicketKind.MERCHANT_LEAD:
                    continue
                if session_id and entry.session_id != session_id:
                    continue
                row = merchants.get(entry.merchant_id or "")
                if row is None:
                    continue
                if entry.direction == "refund":
                    row["refund"] += entry.amount
                    rate = refund_rate_by_order.get(entry.order_id, 0.0)
                    row["gross_payable"] -= int(entry.amount * rate)
                if entry.direction == "payout":
                    row["paid_out"] += entry.amount
            result = []
            for row in merchants.values():
                row["payable"] = max(0, row["gross_payable"]) - row["paid_out"]
                row["orders"] = sorted(row["orders"])
                result.append(row)
            return sorted(result, key=lambda r: r["merchant_id"])

    @staticmethod
    def _entry_dict(entry) -> dict[str, Any]:
        return {
            "id": entry.id, "at": entry.at, "kind": entry.kind.value,
            "direction": entry.direction, "amount": entry.amount,
            "session_id": entry.session_id, "order_id": entry.order_id,
            "merchant_id": entry.merchant_id, "memo": entry.memo,
        }

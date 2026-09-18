"""财务：门票、演出、商户引流分账核清，退款与补偿单独列示。"""

from __future__ import annotations

from .models import yuan

ACCOUNTS = ("ticket", "show", "merchant", "compensation")


def _blank():
    return {"sales": 0, "refunds": 0, "compensations": 0, "net": 0}


class FinanceService:
    def __init__(self, store):
        self.s = store

    def reconciliation(self, event_id=None):
        """按收入类型核清：sales/refunds/compensations/net（元）；商户引流按商户分列。"""
        with self.s.lock:
            accounts = {name: _blank() for name in ACCOUNTS}
            merchants = {}
            for entry in self.s.ledger:
                if event_id is not None and entry.event_id != event_id:
                    continue
                account = accounts.setdefault(entry.account, _blank())
                self._accumulate(account, entry)
                merchant_id = entry.meta.get("merchant_id")
                if merchant_id:
                    merchant = merchants.setdefault(merchant_id, _blank())
                    self._accumulate(merchant, entry)
            return {
                "event_id": event_id,
                "currency": "CNY",
                "accounts": {name: self._to_yuan(values) for name, values in accounts.items()},
                "merchants": {mid: self._to_yuan(values) for mid, values in merchants.items()},
            }

    @staticmethod
    def _accumulate(bucket, entry):
        if entry.kind == "sale":
            bucket["sales"] += entry.amount_cents
        elif entry.kind == "refund":
            bucket["refunds"] += -entry.amount_cents
        elif entry.kind == "compensation":
            bucket["compensations"] += -entry.amount_cents
        bucket["net"] += entry.amount_cents

    @staticmethod
    def _to_yuan(values):
        return {key: yuan(cents) for key, cents in values.items()}

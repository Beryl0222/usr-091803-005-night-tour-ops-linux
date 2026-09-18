"""追加式事件日志。

所有状态变更都先写日志、再改内存（见 :class:`nighttour.store.Store`）。
日志是场次审计还原的唯一权威来源：按 session 过滤即可重建
"容量如何变化、哪些游客被调整、为何允许重新开放" 的完整时间线。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class Event:
    seq: int
    at: float
    type: str
    actor: str
    data: dict[str, Any]
    idempotency_key: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "seq": self.seq,
            "at": self.at,
            "type": self.type,
            "actor": self.actor,
            "data": self.data,
        }
        if self.idempotency_key:
            out["idempotency_key"] = self.idempotency_key
        return out


class EventLog:
    """线程安全的 JSONL 追加日志；未指定路径时仅保留在内存。"""

    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = None):
        self._lock = threading.Lock()
        self._seq = 0
        self._events: list[Event] = []
        self._path = path
        self._clock = clock or _default_clock
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            # 接续已有日志，保证序号连续
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        record = json.loads(line)
                        self._events.append(self._to_event(record))
                if self._events:
                    self._seq = max(e.seq for e in self._events)

    @staticmethod
    def _to_event(record: dict[str, Any]) -> Event:
        return Event(
            seq=record["seq"],
            at=record["at"],
            type=record["type"],
            actor=record.get("actor", "system"),
            data=record.get("data", {}),
            idempotency_key=record.get("idempotency_key"),
        )

    def append(
        self,
        event_type: str,
        data: dict[str, Any],
        actor: str = "system",
        at: Optional[float] = None,
        idempotency_key: Optional[str] = None,
    ) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(
                seq=self._seq,
                at=at if at is not None else self._clock(),
                type=event_type,
                actor=actor,
                data=data,
                idempotency_key=idempotency_key,
            )
            self._events.append(event)
            if self._path:
                self._persist(event)
            return event

    def _persist(self, event: Event) -> None:
        # 先写临时文件再落盘的做法对单行追加过重；此处用行缓冲追加，
        # 崩溃时最多丢失最后一行，且该行不会进入内存状态。
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def for_session(self, session_id: str) -> list[Event]:
        """返回与某场次相关的全部事件（含迁移目标场次）。"""
        result = []
        with self._lock:
            for event in self._events:
                data = event.data
                if data.get("session_id") == session_id:
                    result.append(event)
                elif data.get("from_session_id") == session_id:
                    result.append(event)
                elif session_id in (data.get("session_ids") or []):
                    result.append(event)
        return result

    def has_idempotency_key(self, key: str) -> Optional[Event]:
        with self._lock:
            for event in self._events:
                if event.idempotency_key == key:
                    return event
        return None


def _default_clock() -> float:
    import time

    return time.time()

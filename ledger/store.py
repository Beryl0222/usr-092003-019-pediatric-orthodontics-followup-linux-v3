"""仅追加事件存储。

每件器械的全部事实以事件形式按时间顺序追加；任何事件都不能修改或删除，
因此处方变更、终止、返工只会追加新事件，已经发生的制作事实始终可追溯。
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Actor:
    """请求发起人的最小身份描述。"""

    __slots__ = ("id", "role", "org_id")

    def __init__(self, actor_id, role, org_id=None):
        self.id = actor_id
        self.role = role
        self.org_id = org_id

    def as_dict(self):
        return {"id": self.id, "role": self.role, "org_id": self.org_id}

    @classmethod
    def from_headers(cls, actor_id, role, org_id):
        if not actor_id or not role:
            from .errors import AuthorizationError

            raise AuthorizationError("缺少操作人身份（X-Actor-Id / X-Actor-Role）")
        return cls(actor_id, role, org_id)


class EventStore:
    """线程安全的内存事件存储，可选 JSONL 持久化。"""

    def __init__(self, path=None, clock=None):
        self._lock = threading.RLock()
        self._events = []
        self._streams = {}
        self.path = path
        self.clock = clock or utcnow
        if path and os.path.exists(path):
            self._load()

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    self._apply_loaded(json.loads(line))

    def _apply_loaded(self, event):
        self._events.append(event)
        self._streams.setdefault(event["stream"], []).append(event)

    def append(self, event_type, stream, payload, actor):
        """追加一个事件。stream 为聚合 id（如 device_id）。"""
        event = {
            "event_id": new_id("evt"),
            "ts": self.clock().isoformat(),
            "actor": actor.as_dict(),
            "stream": stream,
            "type": event_type,
            "payload": payload,
        }
        with self._lock:
            event["seq"] = len(self._events) + 1
            event["stream_version"] = len(self._streams.get(stream, ())) + 1
            self._events.append(event)
            self._streams.setdefault(stream, []).append(event)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def events(self, stream=None):
        with self._lock:
            if stream is None:
                return list(self._events)
            return list(self._streams.get(stream, ()))

    def all_events_since(self, seq):
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    @property
    def seq(self):
        with self._lock:
            return len(self._events)

"""Thread-safe publish/subscribe event bus used by the service layer.

Long-running scans emit structured events; the REST/SSE API and the desktop UI
both subscribe.  Events carry a monotonically increasing ``seq`` so clients can
detect gaps after a reconnect.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Event:
    seq: int
    job_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "job_id": self.job_id, "type": self.type, "payload": self.payload}


class EventBus:
    def __init__(self, *, replay_size: int = 512) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[Event]] = []
        self._seq = 0
        self._replay: list[Event] = []
        self._replay_size = replay_size

    def subscribe(self) -> "queue.Queue[Event]":
        q: "queue.Queue[Event]" = queue.Queue(maxsize=2048)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Event]") -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, job_id: str, type_: str, payload: Optional[dict[str, Any]] = None) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(seq=self._seq, job_id=job_id, type=type_, payload=payload or {})
            self._replay.append(event)
            if len(self._replay) > self._replay_size:
                del self._replay[: len(self._replay) - self._replay_size]
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:  # a slow client must not stall a repair
                continue
        return event

    def history(self, since_seq: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._replay if e.seq > since_seq]

    def emitter(self, job_id: str) -> Callable[[str, Optional[dict[str, Any]]], None]:
        def emit(type_: str, payload: Optional[dict[str, Any]] = None) -> None:
            self.publish(job_id, type_, payload)

        return emit

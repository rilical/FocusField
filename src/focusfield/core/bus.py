"""
CONTRACT: inline (source: src/focusfield/core/bus.md)
ROLE: In-process pub/sub with bounded queues.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - bus.max_queue_depth: per-topic queue depth

PERF / TIMING:
  - preserve per-topic ordering

FAILURE MODES:
  - queue full -> drop or backpressure -> log queue_full

LOG EVENTS:
  - module=core.bus, event=queue_full, payload keys=topic, depth

TESTS:
  - tests/contract_tests.md must cover backpressure rules

CONTRACT DETAILS (inline from src/focusfield/core/bus.md):
# Bus contract

- Typed topics with schema validation.
- Backpressure via bounded queues per topic.
- Publish/subscribe is non-blocking where possible.
"""

from __future__ import annotations

import queue
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional


DropHandler = Callable[[str, int], None]


class Bus:
    """Simple in-process pub/sub bus with bounded queues."""

    def __init__(self, max_queue_depth: int = 8, on_drop: Optional[DropHandler] = None) -> None:
        self._max_queue_depth = max_queue_depth
        self._on_drop = on_drop
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._subscribers: Dict[str, List[queue.Queue[Any]]] = defaultdict(list)
        self._drop_counts: Dict[str, int] = defaultdict(int)

    def set_drop_handler(self, on_drop: Optional[DropHandler]) -> None:
        self._on_drop = on_drop

    def get_drop_counts(self) -> Dict[str, int]:
        with self._stats_lock:
            return dict(self._drop_counts)

    def subscribe(self, topic: str) -> queue.Queue[Any]:
        """Subscribe to a topic and return a queue of messages."""
        q: queue.Queue[Any] = queue.Queue(maxsize=self._max_queue_depth)
        with self._lock:
            self._subscribers[topic].append(q)
        return q

    def publish(self, topic: str, msg: Any) -> None:
        """Publish a message to all subscribers without blocking."""
        with self._lock:
            subscribers = list(self._subscribers.get(topic, []))
        for q in subscribers:
            dropped = self._put_with_drop_oldest(q, msg)
            if dropped:
                with self._stats_lock:
                    self._drop_counts[topic] += 1
                if self._on_drop:
                    self._on_drop(topic, q.maxsize)

    @staticmethod
    def _put_with_drop_oldest(q: queue.Queue[Any], msg: Any) -> bool:
        """Enqueue, dropping the oldest item on overflow.

        Returns True when a drop occurred (even if enqueue succeeds).
        """
        try:
            q.put_nowait(msg)
            return False
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(msg)
                return True
            except queue.Full:
                return True

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

import fnmatch
import queue
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional


DropHandler = Callable[[str, int], None]
QueuePolicy = str


class Bus:
    """Simple in-process pub/sub bus with bounded queues."""

    def __init__(
        self,
        max_queue_depth: int = 8,
        on_drop: Optional[DropHandler] = None,
        topic_queue_depths: Optional[Dict[str, int]] = None,
        topic_queue_policies: Optional[Dict[str, QueuePolicy]] = None,
    ) -> None:
        self._max_queue_depth = max_queue_depth
        self._on_drop = on_drop
        self._topic_queue_depths = dict(topic_queue_depths or {})
        self._topic_queue_policies = {str(key): str(value) for key, value in dict(topic_queue_policies or {}).items()}
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._subscribers: Dict[str, List[queue.Queue[Any]]] = defaultdict(list)
        self._drop_counts: Dict[str, int] = defaultdict(int)
        self._publish_counts: Dict[str, int] = defaultdict(int)

    def set_drop_handler(self, on_drop: Optional[DropHandler]) -> None:
        self._on_drop = on_drop

    def get_drop_counts(self) -> Dict[str, int]:
        with self._stats_lock:
            return dict(self._drop_counts)

    def get_publish_counts(self) -> Dict[str, int]:
        with self._stats_lock:
            return dict(self._publish_counts)

    def get_topic_stats(self) -> Dict[str, Dict[str, int]]:
        with self._stats_lock:
            topics = set(self._drop_counts.keys()) | set(self._publish_counts.keys())
            return {
                topic: {
                    "published": int(self._publish_counts.get(topic, 0)),
                    "dropped": int(self._drop_counts.get(topic, 0)),
                }
                for topic in sorted(topics)
            }

    def subscribe(self, topic: str) -> queue.Queue[Any]:
        """Subscribe to a topic and return a queue of messages."""
        topic_depth = self._resolve_topic_depth(topic)
        q: queue.Queue[Any] = queue.Queue(maxsize=topic_depth)
        with self._lock:
            self._subscribers[topic].append(q)
        return q

    def get_topic_depth(self, topic: str) -> int:
        """Return the effective queue depth for a topic."""
        return self._resolve_topic_depth(topic)

    def _resolve_topic_depth(self, topic: str) -> int:
        configured_depth = self._topic_queue_depths.get(topic)
        if configured_depth is not None:
            return self._clamp_topic_depth(configured_depth, self._max_queue_depth)

        wildcard_matches: list[tuple[str, Any]] = []
        for key, value in self._topic_queue_depths.items():
            if "*" in str(key) and fnmatch.fnmatch(topic, str(key)):
                wildcard_matches.append((str(key), value))

        if wildcard_matches:
            best_key, best_value = max(wildcard_matches, key=lambda item: len(item[0]))
            return self._clamp_topic_depth(best_value, self._max_queue_depth)

        return max(1, int(self._max_queue_depth))

    @staticmethod
    def _clamp_topic_depth(value: Any, fallback: int) -> int:
        try:
            depth = int(value)
        except Exception:
            return max(1, int(fallback))
        if depth <= 0:
            return max(1, int(fallback))
        return max(1, depth)

    def publish(self, topic: str, msg: Any) -> None:
        """Publish a message to all subscribers without blocking."""
        with self._lock:
            subscribers = list(self._subscribers.get(topic, []))
        with self._stats_lock:
            self._publish_counts[topic] += 1
        policy = self._resolve_topic_policy(topic)
        for q in subscribers:
            dropped = self._put_with_policy(q, msg, policy)
            if dropped:
                with self._stats_lock:
                    self._drop_counts[topic] += 1
                if self._on_drop:
                    self._on_drop(topic, q.maxsize)

    def _resolve_topic_policy(self, topic: str) -> str:
        configured = self._topic_queue_policies.get(topic)
        if configured is not None:
            return self._normalize_policy(configured)
        wildcard_matches: list[tuple[str, Any]] = []
        for key, value in self._topic_queue_policies.items():
            if "*" in str(key) and fnmatch.fnmatch(topic, str(key)):
                wildcard_matches.append((str(key), value))
        if wildcard_matches:
            _best_key, best_value = max(wildcard_matches, key=lambda item: len(item[0]))
            return self._normalize_policy(best_value)
        return "drop_oldest"

    @staticmethod
    def _normalize_policy(value: Any) -> str:
        policy = str(value or "drop_oldest").strip().lower()
        if policy in {"drop_newest", "newest"}:
            return "drop_newest"
        return "drop_oldest"

    @staticmethod
    def _put_with_policy(q: queue.Queue[Any], msg: Any, policy: str) -> bool:
        """Enqueue, applying the configured overflow policy.

        Returns True when a drop occurred (even if enqueue succeeds).
        """
        try:
            q.put_nowait(msg)
            return False
        except queue.Full:
            if policy == "drop_newest":
                return True
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(msg)
                return True
            except queue.Full:
                return True

"""
CONTRACT: inline (source: src/focusfield/fusion/lock_state_machine.md)
ROLE: Target lock state machine with hysteresis.

INPUTS:
  - Topic: fusion.candidates  Type: AssociationCandidate[]
OUTPUTS:
  - Topic: fusion.target_lock  Type: TargetLock

CONFIG KEYS:
  - fusion.acquire_threshold: acquire threshold
  - fusion.hold_ms: hold duration
  - fusion.handoff_min_ms: handoff minimum
  - fusion.drop_threshold: drop threshold

PERF / TIMING:
  - per update; deterministic transitions

FAILURE MODES:
  - invalid transition -> log invalid_transition

LOG EVENTS:
  - module=fusion.lock_state_machine, event=invalid_transition, payload keys=state, reason

TESTS:
  - tests/lock_logic_sanity.md must cover hysteresis and handoff timing

CONTRACT DETAILS (inline from src/focusfield/fusion/lock_state_machine.md):
# Lock state machine

## States

- NO_LOCK, ACQUIRE, LOCKED, HOLD, HANDOFF.

## Transitions

- Acquire when mouth activity or VAD indicates speech.
- Lock when AV association confidence >= acquire_threshold.
- Hold during short silences up to hold_ms.
- Handoff when a new candidate dominates for handoff_min_ms.

## Failure handling

- Vision fails: rely on audio confidence until timeout.
- Audio confidence drops: fall back to HOLD or NO_LOCK.
- Disagreement: reduce confidence and avoid rapid switching.

## Output

- TargetLock includes reason strings for debugging and demos.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List, Optional

from focusfield.core.clock import now_ns


class LockStateMachine:
    """Vision-first lock state machine with hysteresis."""

    def __init__(self, config: Dict[str, Any]) -> None:
        thresholds = config.get("fusion", {}).get("thresholds", {})
        self._acquire = float(thresholds.get("acquire_threshold", 0.65))
        self._hold_ms = float(thresholds.get("hold_ms", 800))
        self._handoff_min_ms = float(thresholds.get("handoff_min_ms", 700))
        self._drop = float(thresholds.get("drop_threshold", self._acquire * 0.6))
        self._state = "NO_LOCK"
        self._target_id: Optional[str] = None
        self._target_bearing: Optional[float] = None
        self._last_score = 0.0
        self._last_seen_ns = 0
        self._handoff_id: Optional[str] = None
        self._handoff_start_ns = 0
        self._seq = 0

    def update(self, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        t_ns = now_ns()
        best = _best_candidate(candidates)
        reason = "no_candidates"

        if self._state == "NO_LOCK":
            if best and best["combined_score"] >= self._acquire:
                self._lock_to(best, t_ns)
                reason = "acquired"
            else:
                self._clear()
        else:
            if not best:
                if self._within_hold(t_ns):
                    self._state = "HOLD"
                    reason = "hold_no_candidates"
                else:
                    self._clear()
                    reason = "drop_no_candidates"
            elif best["track_id"] == self._target_id:
                self._state = "LOCKED"
                self._last_seen_ns = t_ns
                self._last_score = float(best["combined_score"])
                self._target_bearing = float(best.get("bearing_deg", 0.0))
                reason = "maintain"
            else:
                reason = self._maybe_handoff(best, t_ns)

        self._seq += 1
        return {
            "t_ns": t_ns,
            "seq": self._seq,
            "state": self._state,
            "mode": "VISION_ONLY" if self._state != "NO_LOCK" else "NO_LOCK",
            "target_id": self._target_id,
            "target_bearing_deg": self._target_bearing,
            "confidence": float(self._last_score) if self._state != "NO_LOCK" else 0.0,
            "reason": reason,
            "stability": {
                "hold_ms": self._hold_ms,
                "handoff_ms": self._handoff_min_ms,
            },
        }

    def _lock_to(self, candidate: Dict[str, Any], t_ns: int) -> None:
        self._state = "LOCKED"
        self._target_id = candidate["track_id"]
        self._target_bearing = float(candidate.get("bearing_deg", 0.0))
        self._last_score = float(candidate["combined_score"])
        self._last_seen_ns = t_ns
        self._handoff_id = None
        self._handoff_start_ns = 0

    def _clear(self) -> None:
        self._state = "NO_LOCK"
        self._target_id = None
        self._target_bearing = None
        self._last_score = 0.0
        self._last_seen_ns = 0
        self._handoff_id = None
        self._handoff_start_ns = 0

    def _within_hold(self, t_ns: int) -> bool:
        return self._last_seen_ns and (t_ns - self._last_seen_ns) <= int(self._hold_ms * 1_000_000)

    def _maybe_handoff(self, best: Dict[str, Any], t_ns: int) -> str:
        score = float(best["combined_score"])
        if score < self._drop and not self._within_hold(t_ns):
            self._clear()
            return "drop_low_confidence"
        if self._handoff_id != best["track_id"]:
            self._handoff_id = best["track_id"]
            self._handoff_start_ns = t_ns
            self._state = "HANDOFF"
            return "handoff_start"
        if (t_ns - self._handoff_start_ns) >= int(self._handoff_min_ms * 1_000_000) and score >= self._acquire:
            self._lock_to(best, t_ns)
            return "handoff_commit"
        self._state = "HANDOFF"
        return "handoff_wait"


def start_lock_state_machine(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    q = bus.subscribe("fusion.candidates")
    machine = LockStateMachine(config)

    def _run() -> None:
        while not stop_event.is_set():
            try:
                candidates = q.get(timeout=0.1)
            except queue.Empty:
                continue
            msg = machine.update(candidates)
            bus.publish("fusion.target_lock", msg)

    thread = threading.Thread(target=_run, name="lock-state", daemon=True)
    thread.start()
    return thread


def _best_candidate(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    return max(candidates, key=lambda cand: float(cand.get("combined_score", 0.0)))

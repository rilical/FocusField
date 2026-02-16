"""
CONTRACT: inline (source: src/focusfield/fusion/lock_state_machine.md)
ROLE: Target lock state machine with hysteresis.

INPUTS:
  - Topic: fusion.candidates  Type: AssociationCandidate[]
OUTPUTS:
  - Topic: fusion.target_lock  Type: TargetLock

CONFIG KEYS:
  - fusion.thresholds.acquire_threshold: acquire threshold
  - fusion.thresholds.acquire_timeout_ms: max time to stay in ACQUIRE before dropping
  - fusion.thresholds.hold_ms: hold duration
  - fusion.thresholds.handoff_min_ms: handoff minimum
  - fusion.thresholds.drop_threshold: drop threshold
  - fusion.thresholds.speak_on_threshold: speaking gating threshold
  - fusion.thresholds.min_switch_interval_ms: minimum time between target switches
  - fusion.thresholds.bearing_smoothing_alpha: target bearing smoothing
  - fusion.priority_policy: confidence_only | confidence_then_recency | recency
  - fusion.preferred_track_id: optional explicit target selection
  - fusion.recency_decay_ms: recency bonus decay window for policy

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

## Priority policy

- confidence_only: pick highest combined_score.
- confidence_then_recency: break ties with recent speakers.
- recency: prefer most recent speaker, break ties with confidence.
- preferred_track_id overrides any policy when present in the candidate pool.
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
        self._acquire_timeout_ms = float(thresholds.get("acquire_timeout_ms", 500))
        self._hold_ms = float(thresholds.get("hold_ms", 800))
        self._handoff_min_ms = float(thresholds.get("handoff_min_ms", 700))
        self._drop = float(thresholds.get("drop_threshold", self._acquire * 0.6))
        self._speak_on = float(thresholds.get("speak_on_threshold", 0.5))
        self._min_switch_interval_ms = float(thresholds.get("min_switch_interval_ms", 500))
        self._bearing_alpha = float(thresholds.get("bearing_smoothing_alpha", 0.7))
        self._require_vad = bool(config.get("fusion", {}).get("require_vad", False))
        self._vad_max_age_ms = float(config.get("fusion", {}).get("vad_max_age_ms", 500))
        self._require_speaking = bool(config.get("fusion", {}).get("require_speaking", True))
        self._priority_policy = str(config.get("fusion", {}).get("priority_policy", "confidence_then_recency"))
        self._preferred_track_id = config.get("fusion", {}).get("preferred_track_id")
        self._recency_decay_ms = float(config.get("fusion", {}).get("recency_decay_ms", 1200))
        self._state = "NO_LOCK"
        self._target_id: Optional[str] = None
        self._target_bearing: Optional[float] = None
        self._target_mode: str = "NO_LOCK"
        self._last_score = 0.0
        self._last_seen_ns = 0
        self._last_speaking_ns = 0
        self._last_speaking_by_track: Dict[str, int] = {}
        self._handoff_id: Optional[str] = None
        self._handoff_start_ns = 0
        self._acquire_start_ns = 0
        self._last_switch_ns = 0
        self._seq = 0

    def update(self, candidates: List[Dict[str, Any]], vad_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        t_ns = now_ns()
        self._update_speaking_history(candidates, t_ns)
        has_speaking = _has_speaking_candidate(candidates, self._speak_on)
        vad_speech = bool(vad_state.get("speech")) if _vad_is_fresh(vad_state, t_ns, self._vad_max_age_ms) and vad_state is not None else False
        if has_speaking:
            self._last_speaking_ns = t_ns
        if self._require_vad and _vad_is_fresh(vad_state, t_ns, self._vad_max_age_ms):
            if not bool(vad_state.get("speech")) and not has_speaking:
                if self._state in {"LOCKED", "HOLD", "HANDOFF"} and self._within_hold(t_ns):
                    self._state = "HOLD"
                    reason = "vad_hold"
                else:
                    self._clear()
                    reason = "vad_silence"
                self._seq += 1
                return {
                    "t_ns": t_ns,
                    "seq": self._seq,
                    "state": self._state,
                    "mode": self._target_mode if self._state != "NO_LOCK" else "NO_LOCK",
                    "target_id": self._target_id,
                    "target_bearing_deg": self._target_bearing,
                    "confidence": float(self._last_score) if self._state != "NO_LOCK" else 0.0,
                    "reason": reason,
                    "stability": {
                        "hold_ms": self._hold_ms,
                        "handoff_ms": self._handoff_min_ms,
                    },
                }
        if self._require_speaking and not (has_speaking or vad_speech):
            if self._state in {"LOCKED", "HOLD", "HANDOFF"} and self._within_speech_hold(t_ns):
                reason = "silence_hold"
                self._state = "HOLD"
            else:
                reason = "silence_drop"
                self._clear()
            self._seq += 1
            return {
                "t_ns": t_ns,
                "seq": self._seq,
                "state": self._state,
                "mode": self._target_mode if self._state != "NO_LOCK" else "NO_LOCK",
                "target_id": self._target_id,
                "target_bearing_deg": self._target_bearing,
                "confidence": float(self._last_score) if self._state != "NO_LOCK" else 0.0,
                "reason": reason,
                "stability": {
                    "hold_ms": self._hold_ms,
                    "handoff_ms": self._handoff_min_ms,
                },
            }

        best = _best_candidate(
            candidates,
            self._speak_on,
            self._require_speaking and not vad_speech,
            self._priority_policy,
            self._preferred_track_id,
            self._last_speaking_by_track,
            self._recency_decay_ms,
        )
        reason = "no_candidates"

        if self._state == "NO_LOCK":
            if best is None:
                self._clear()
            else:
                score = float(best.get("combined_score", 0.0))
                if score >= self._acquire:
                    if self._lock_to(best, t_ns):
                        reason = "acquired"
                    else:
                        reason = "switch_throttled"
                else:
                    # Speech is present but confidence is still building.
                    self._state = "ACQUIRE"
                    self._acquire_start_ns = t_ns
                    self._target_id = str(best.get("track_id")) if best.get("track_id") is not None else None
                    self._target_bearing = float(best.get("bearing_deg", 0.0)) % 360.0
                    self._last_score = score
                    self._target_mode = _infer_mode(best)
                    reason = "acquire_start"
        elif self._state == "ACQUIRE":
            if best is None:
                self._clear()
                reason = "drop_no_candidates"
            else:
                if self._acquire_start_ns and (t_ns - self._acquire_start_ns) >= int(self._acquire_timeout_ms * 1_000_000):
                    self._clear()
                    reason = "acquire_timeout"
                else:
                    score = float(best.get("combined_score", 0.0))
                    if score >= self._acquire:
                        if self._lock_to(best, t_ns):
                            reason = "acquired"
                        else:
                            reason = "switch_throttled"
                        self._acquire_start_ns = 0
                    else:
                        self._state = "ACQUIRE"
                        self._target_id = str(best.get("track_id")) if best.get("track_id") is not None else None
                        self._target_bearing = float(best.get("bearing_deg", 0.0)) % 360.0
                        self._last_score = score
                        self._target_mode = _infer_mode(best)
                        reason = "acquire_wait"
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
                self._target_bearing = _smooth_angle(self._target_bearing, float(best.get("bearing_deg", 0.0)), self._bearing_alpha)
                self._target_mode = _infer_mode(best)
                reason = "maintain"
            else:
                reason = self._maybe_handoff(best, t_ns)

        self._seq += 1
        return {
            "t_ns": t_ns,
            "seq": self._seq,
            "state": self._state,
            "mode": self._target_mode if self._state != "NO_LOCK" else "NO_LOCK",
            "target_id": self._target_id,
            "target_bearing_deg": self._target_bearing,
            "confidence": float(self._last_score) if self._state != "NO_LOCK" else 0.0,
            "reason": reason,
            "stability": {
                "hold_ms": self._hold_ms,
                "handoff_ms": self._handoff_min_ms,
            },
        }

    def _lock_to(self, candidate: Dict[str, Any], t_ns: int) -> bool:
        if self._target_id is not None and candidate["track_id"] != self._target_id:
            if self._last_switch_ns and (t_ns - self._last_switch_ns) < int(self._min_switch_interval_ms * 1_000_000):
                return False
        self._state = "LOCKED"
        self._target_id = candidate["track_id"]
        self._target_bearing = _smooth_angle(self._target_bearing, float(candidate.get("bearing_deg", 0.0)), self._bearing_alpha)
        self._target_mode = _infer_mode(candidate)
        self._last_score = float(candidate["combined_score"])
        self._last_seen_ns = t_ns
        self._handoff_id = None
        self._handoff_start_ns = 0
        self._acquire_start_ns = 0
        self._last_switch_ns = t_ns
        return True

    def _clear(self) -> None:
        self._state = "NO_LOCK"
        self._target_id = None
        self._target_bearing = None
        self._target_mode = "NO_LOCK"
        self._last_score = 0.0
        self._last_seen_ns = 0
        self._last_speaking_ns = 0
        self._handoff_id = None
        self._handoff_start_ns = 0
        self._acquire_start_ns = 0

    def _within_hold(self, t_ns: int) -> bool:
        return self._last_seen_ns and (t_ns - self._last_seen_ns) <= int(self._hold_ms * 1_000_000)

    def _within_speech_hold(self, t_ns: int) -> bool:
        return self._last_speaking_ns and (t_ns - self._last_speaking_ns) <= int(self._hold_ms * 1_000_000)

    def _update_speaking_history(self, candidates: List[Dict[str, Any]], t_ns: int) -> None:
        for cand in candidates:
            track_id = cand.get("track_id")
            if track_id is None:
                continue
            if cand.get("speaking") or float(cand.get("score_components", {}).get("mouth_activity", 0.0)) >= self._speak_on:
                self._last_speaking_by_track[str(track_id)] = t_ns

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
            if self._lock_to(best, t_ns):
                return "handoff_commit"
            self._state = "LOCKED"
            return "switch_throttled"
        self._state = "HANDOFF"
        return "handoff_wait"


def start_lock_state_machine(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    q = bus.subscribe("fusion.candidates")
    q_vad = bus.subscribe("audio.vad")
    machine = LockStateMachine(config)
    last_vad: Optional[Dict[str, Any]] = None

    def _run() -> None:
        nonlocal last_vad
        while not stop_event.is_set():
            try:
                candidates = q.get(timeout=0.1)
            except queue.Empty:
                candidates = None
            try:
                while True:
                    last_vad = q_vad.get_nowait()
            except queue.Empty:
                pass
            if candidates is None:
                continue
            msg = machine.update(candidates, last_vad)
            bus.publish("fusion.target_lock", msg)

    thread = threading.Thread(target=_run, name="lock-state", daemon=True)
    thread.start()
    return thread


def _best_candidate(
    candidates: List[Dict[str, Any]],
    speak_on_threshold: float,
    require_speaking: bool,
    priority_policy: str,
    preferred_track_id: Optional[str],
    last_speaking_by_track: Dict[str, int],
    recency_decay_ms: float,
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    speaking = [
        cand
        for cand in candidates
        if cand.get("speaking") or float(cand.get("score_components", {}).get("mouth_activity", 0.0)) >= speak_on_threshold
    ]
    if require_speaking and not speaking:
        return None
    pool = speaking or candidates
    if preferred_track_id is not None:
        preferred_id = str(preferred_track_id)
        for cand in pool:
            if str(cand.get("track_id")) == preferred_id:
                return cand
    policy = str(priority_policy or "").lower()
    if policy == "recency":
        return max(
            pool,
            key=lambda cand: (
                _priority_score(cand, policy, preferred_track_id, last_speaking_by_track, recency_decay_ms),
                float(cand.get("combined_score", 0.0)),
            ),
        )
    if policy == "confidence_only":
        return max(pool, key=lambda cand: float(cand.get("combined_score", 0.0)))
    return max(
        pool,
        key=lambda cand: (
            float(cand.get("combined_score", 0.0)),
            _priority_score(cand, policy, preferred_track_id, last_speaking_by_track, recency_decay_ms),
        ),
    )


def _priority_score(
    candidate: Dict[str, Any],
    priority_policy: str,
    preferred_track_id: Optional[str],
    last_speaking_by_track: Dict[str, int],
    recency_decay_ms: float,
) -> float:
    """Priority arbitration for multi-speaker cases."""
    track_id = str(candidate.get("track_id", ""))
    base = 0.0
    if preferred_track_id and track_id == str(preferred_track_id):
        base += 10.0
    if priority_policy == "confidence_only":
        return base
    if priority_policy in {"confidence_then_recency", "recency"}:
        last_ns = last_speaking_by_track.get(track_id)
        if last_ns:
            age_ms = max(0.0, (now_ns() - last_ns) / 1_000_000.0)
            if recency_decay_ms > 0:
                base += max(0.0, 1.0 - (age_ms / recency_decay_ms))
    return base


def _has_speaking_candidate(candidates: List[Dict[str, Any]], speak_on_threshold: float) -> bool:
    for cand in candidates:
        if cand.get("speaking"):
            return True
        if float(cand.get("score_components", {}).get("mouth_activity", 0.0)) >= speak_on_threshold:
            return True
    return False


def _smooth_angle(previous: Optional[float], new: float, alpha: float) -> float:
    if previous is None:
        return new % 360.0
    delta = (new - previous + 180.0) % 360.0 - 180.0
    return (previous + alpha * delta) % 360.0


def _vad_is_fresh(vad_state: Optional[Dict[str, Any]], t_ns: int, max_age_ms: float) -> bool:
    if vad_state is None:
        return False
    vad_t = vad_state.get("t_ns")
    if vad_t is None:
        return False
    return (t_ns - int(vad_t)) <= int(max_age_ms * 1_000_000)


def _infer_mode(candidate: Dict[str, Any]) -> str:
    """Infer TargetLock.mode from candidate evidence."""
    try:
        track_id = str(candidate.get("track_id", "") or "")
    except Exception:  # noqa: BLE001
        track_id = ""
    if track_id.startswith("audio:"):
        return "AUDIO_ONLY"
    if candidate.get("doa_peak_deg") is not None:
        return "AV_LOCK"
    return "VISION_ONLY"

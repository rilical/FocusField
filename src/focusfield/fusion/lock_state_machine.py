"""
CONTRACT: inline (source: src/focusfield/fusion/lock_state_machine.md)
ROLE: Target lock state machine with hysteresis.

INPUTS:
  - Topic: fusion.candidates  Type: FusionCandidatesEnvelope (preferred) or AssociationCandidate[] for compatibility
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

- confidence_only: pick highest focus_score.
- confidence_then_recency: break ties with recent speakers.
- recency: prefer most recent speaker, break ties with confidence.
- preferred_track_id overrides any policy when present in the candidate pool.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List, Optional, TypedDict

from focusfield.core.clock import now_ns


class FusionCandidatesEnvelope(TypedDict):
    candidates: List[Dict[str, Any]]
    evidence: Dict[str, Any]


CandidatesPayload = FusionCandidatesEnvelope | List[Dict[str, Any]]


class LockStateMachine:
    """Vision-first lock state machine with hysteresis."""

    def __init__(self, config: Dict[str, Any]) -> None:
        thresholds = config.get("fusion", {}).get("thresholds", {})
        self._acquire = float(thresholds.get("acquire_threshold", 0.65))
        self._acquire_timeout_ms = float(thresholds.get("acquire_timeout_ms", 500))
        self._acquire_persist_ms = max(0.0, float(thresholds.get("acquire_persist_ms", 0.0)))
        self._acquire_floor_ratio = min(1.0, max(0.0, float(thresholds.get("acquire_floor_ratio", 1.0))))
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
        self._visual_freshness_ms = float(config.get("fusion", {}).get("visual_freshness_ms", 1200.0) or 1200.0)
        self._visual_override_min = float(config.get("fusion", {}).get("visual_override_min", 0.6) or 0.6)
        self._audio_rescue_min = float(config.get("fusion", {}).get("audio_rescue_min", self._acquire) or self._acquire)
        self._state = "NO_LOCK"
        self._target_id: Optional[str] = None
        self._target_camera_id: Optional[str] = None
        self._target_bearing: Optional[float] = None
        self._target_mode: str = "NO_LOCK"
        self._last_score = 0.0
        self._last_activity_score = 0.0
        self._last_score_margin = 0.0
        self._last_runner_up_score = 0.0
        self._last_seen_ns = 0
        self._last_speaking_ns = 0
        self._last_speaking_by_track: Dict[str, int] = {}
        self._handoff_id: Optional[str] = None
        self._handoff_start_ns = 0
        self._acquire_start_ns = 0
        self._last_switch_ns = 0
        self._seq = 0

    def update(self, candidates: CandidatesPayload, vad_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        t_ns = now_ns()
        candidate_list, evidence = _unwrap_candidates_payload(candidates)
        self._update_speaking_history(candidate_list, t_ns)
        has_speaking = _has_speaking_candidate(candidate_list, self._speak_on)
        vad_fresh = _vad_is_fresh(vad_state, t_ns, self._vad_max_age_ms)
        vad_speech = bool(vad_state.get("speech")) if vad_fresh and vad_state is not None else False
        if has_speaking:
            self._last_speaking_ns = t_ns
        if self._require_vad and not vad_fresh and not has_speaking:
            if self._state in {"LOCKED", "HOLD", "HANDOFF"} and self._within_hold(t_ns):
                self._state = "HOLD"
                reason = "audio_stale_hold"
            else:
                self._clear()
                reason = "audio_stale_drop"
            self._seq += 1
            return self._build_output(t_ns, reason, evidence)
        if self._require_vad and vad_fresh:
            if not bool(vad_state.get("speech")) and not has_speaking:
                if self._state in {"LOCKED", "HOLD", "HANDOFF"} and self._within_hold(t_ns):
                    self._state = "HOLD"
                    reason = "vad_hold"
                else:
                    self._clear()
                    reason = "vad_silence"
                self._seq += 1
                return self._build_output(t_ns, reason, evidence)
        if self._require_speaking and not (has_speaking or vad_speech):
            visual_stale = bool(evidence.get("visual_stale", False))
            if self._state in {"LOCKED", "HOLD", "HANDOFF"} and self._within_speech_hold(t_ns):
                reason = "visual_stale_hold" if visual_stale else "silence_hold"
                self._state = "HOLD"
            else:
                reason = "visual_stale_drop" if visual_stale else "silence_drop"
                self._clear()
            self._seq += 1
            return self._build_output(t_ns, reason, evidence)

        best = _best_candidate(
            candidate_list,
            self._speak_on,
            self._require_speaking and not vad_speech,
            self._priority_policy,
            self._preferred_track_id,
            self._last_speaking_by_track,
            self._recency_decay_ms,
            self._visual_override_min,
        )
        runner_up_score = _runner_up_focus_score(candidate_list, best)
        reason = "no_candidates"

        if self._state == "NO_LOCK":
            if best is None:
                self._clear()
            else:
                score = _candidate_focus_score(best)
                if score >= self._acquire:
                    if self._lock_to(best, t_ns):
                        self._update_selected_metrics(best, runner_up_score)
                        reason = "acquired"
                    else:
                        reason = "acquire_switch_throttled"
                else:
                    # Speech is present but confidence is still building.
                    self._state = "ACQUIRE"
                    self._acquire_start_ns = t_ns
                    self._target_id = str(best.get("track_id")) if best.get("track_id") is not None else None
                    self._target_camera_id = _candidate_camera_id(best)
                    self._target_bearing = _candidate_steering_bearing(best)
                    self._last_score = score
                    self._update_selected_metrics(best, runner_up_score)
                    self._target_mode = _infer_mode(best)
                    reason = "acquire_start"
        elif self._state == "ACQUIRE":
            if best is None:
                self._clear()
                reason = "visual_stale_drop" if bool(evidence.get("visual_stale", False)) else "drop_no_candidates"
            else:
                if self._acquire_start_ns and (t_ns - self._acquire_start_ns) >= int(self._acquire_timeout_ms * 1_000_000):
                    self._clear()
                    reason = "acquire_timeout"
                else:
                    score = _candidate_focus_score(best)
                    if score >= self._acquire:
                        if self._lock_to(best, t_ns):
                            self._update_selected_metrics(best, runner_up_score)
                            reason = "acquired"
                        else:
                            reason = "acquire_switch_throttled"
                        self._acquire_start_ns = 0
                    elif self._can_acquire_persist(best, score, t_ns):
                        if self._lock_to(best, t_ns):
                            self._update_selected_metrics(best, runner_up_score)
                            reason = "acquired_persist"
                        else:
                            reason = "acquire_switch_throttled"
                        self._acquire_start_ns = 0
                    else:
                        self._state = "ACQUIRE"
                        self._target_id = str(best.get("track_id")) if best.get("track_id") is not None else None
                        self._target_camera_id = _candidate_camera_id(best)
                        self._target_bearing = _candidate_steering_bearing(best)
                        self._last_score = score
                        self._update_selected_metrics(best, runner_up_score)
                        self._target_mode = _infer_mode(best)
                        reason = "acquire_wait"
        else:
            if not best:
                if self._within_hold(t_ns):
                    self._state = "HOLD"
                    reason = "visual_stale_hold" if bool(evidence.get("visual_stale", False)) else "hold_no_candidates"
                else:
                    self._clear()
                    reason = "visual_stale_drop" if bool(evidence.get("visual_stale", False)) else "drop_no_candidates"
            elif best["track_id"] == self._target_id:
                self._state = "LOCKED"
                self._last_seen_ns = t_ns
                self._last_score = _candidate_focus_score(best)
                self._update_selected_metrics(best, runner_up_score)
                self._target_camera_id = _candidate_camera_id(best)
                self._target_bearing = _smooth_angle(self._target_bearing, _candidate_steering_bearing(best), self._bearing_alpha)
                self._target_mode = _infer_mode(best)
                reason = "maintain"
            else:
                reason = self._maybe_handoff(best, t_ns, runner_up_score)

        self._seq += 1
        return self._build_output(t_ns, reason, evidence)

    def _build_output(self, t_ns: int, reason: str, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        focus_score = float(self._last_score) if self._state != "NO_LOCK" else 0.0
        evidence = evidence if isinstance(evidence, dict) else {}
        return {
            "t_ns": t_ns,
            "seq": self._seq,
            "state": self._state,
            "mode": self._target_mode if self._state != "NO_LOCK" else "NO_LOCK",
            "target_id": self._target_id,
            "target_camera_id": self._target_camera_id,
            "target_bearing_deg": self._target_bearing,
            "angle_deg": self._target_bearing,
            "confidence": focus_score,
            "focus_score": focus_score,
            "activity_score": float(self._last_activity_score) if self._state != "NO_LOCK" else 0.0,
            "selection_mode": self._target_mode if self._state != "NO_LOCK" else "NO_LOCK",
            "score_margin": float(self._last_score_margin) if self._state != "NO_LOCK" else 0.0,
            "runner_up_focus_score": float(self._last_runner_up_score) if self._state != "NO_LOCK" else 0.0,
            "reason": reason,
            "active_thresholds": {
                "acquire": self._acquire,
                "drop": self._drop,
                "speak_on": self._speak_on,
                "visual_override_min": self._visual_override_min,
                "audio_rescue_min": self._audio_rescue_min,
            },
            "timing_window_ms": {
                "hold_ms": self._hold_ms,
                "handoff_min_ms": self._handoff_min_ms,
                "acquire_timeout_ms": self._acquire_timeout_ms,
                "acquire_persist_ms": self._acquire_persist_ms,
                "min_switch_interval_ms": self._min_switch_interval_ms,
                "visual_freshness_ms": self._visual_freshness_ms,
            },
            "evidence_status": {
                "visual_fresh": evidence.get("faces_fresh"),
                "visual_stale": evidence.get("visual_stale"),
                "audio_fresh": evidence.get("audio_fresh"),
                "audio_stale": evidence.get("audio_stale"),
                "disagreement_suppressed": evidence.get("disagreement_suppressed", False),
                "source_reason": evidence.get("reason", ""),
            },
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
        self._target_camera_id = _candidate_camera_id(candidate)
        self._target_bearing = _smooth_angle(self._target_bearing, _candidate_steering_bearing(candidate), self._bearing_alpha)
        self._target_mode = _infer_mode(candidate)
        self._last_score = _candidate_focus_score(candidate)
        self._last_seen_ns = t_ns
        self._handoff_id = None
        self._handoff_start_ns = 0
        self._acquire_start_ns = 0
        self._last_switch_ns = t_ns
        return True

    def _update_selected_metrics(self, candidate: Dict[str, Any], runner_up_score: float) -> None:
        self._last_activity_score = _candidate_activity_score(candidate)
        self._last_runner_up_score = float(max(0.0, runner_up_score))
        self._last_score_margin = max(0.0, _candidate_focus_score(candidate) - self._last_runner_up_score)

    def _clear(self) -> None:
        self._state = "NO_LOCK"
        self._target_id = None
        self._target_camera_id = None
        self._target_bearing = None
        self._target_mode = "NO_LOCK"
        self._last_score = 0.0
        self._last_activity_score = 0.0
        self._last_score_margin = 0.0
        self._last_runner_up_score = 0.0
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
            if cand.get("speaking") or _candidate_speaking_probability(cand) >= self._speak_on:
                self._last_speaking_by_track[str(track_id)] = t_ns

    def _maybe_handoff(self, best: Dict[str, Any], t_ns: int, runner_up_score: float) -> str:
        score = _candidate_focus_score(best)
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
                self._update_selected_metrics(best, runner_up_score)
                return "handoff_commit"
            self._state = "LOCKED"
            return "handoff_switch_throttled"
        self._state = "HANDOFF"
        return "handoff_wait"

    def _can_acquire_persist(self, candidate: Dict[str, Any], score: float, t_ns: int) -> bool:
        if self._acquire_persist_ms <= 0.0 or self._acquire_floor_ratio >= 1.0:
            return False
        if self._acquire_start_ns <= 0:
            return False
        if self._target_id is None or str(candidate.get("track_id")) != self._target_id:
            return False
        elapsed_ns = t_ns - self._acquire_start_ns
        if elapsed_ns < int(self._acquire_persist_ms * 1_000_000):
            return False
        acquire_floor = self._acquire * self._acquire_floor_ratio
        return score >= acquire_floor


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
    visual_override_min: float,
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    speaking = [
        cand
        for cand in candidates
        if cand.get("speaking") or _candidate_speaking_probability(cand) >= speak_on_threshold
    ]
    if require_speaking and not speaking:
        return None
    pool = speaking or candidates
    visual_override_pool = [
        cand
        for cand in pool
        if _infer_mode(cand) != "AUDIO_ONLY" and _candidate_visual_score(cand) >= visual_override_min
    ]
    if visual_override_pool:
        pool = visual_override_pool
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
                _candidate_focus_score(cand),
            ),
        )
    if policy == "confidence_only":
        return max(pool, key=_candidate_focus_score)
    return max(
        pool,
        key=lambda cand: (
            _candidate_focus_score(cand),
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
        if _candidate_speaking_probability(cand) >= speak_on_threshold:
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


def _candidate_speaking_probability(candidate: Dict[str, Any]) -> float:
    score_components = candidate.get("score_components", {})
    if not isinstance(score_components, dict):
        score_components = {}
    raw = candidate.get(
        "speaking_probability",
        score_components.get(
            "visual_speaking_prob",
            score_components.get("mouth_activity", 0.0),
        ),
    )
    try:
        probability = float(raw or 0.0)
    except Exception:
        return 0.0
    if probability < 0.0:
        return 0.0
    if probability > 1.0:
        return 1.0
    return probability


def _candidate_focus_score(candidate: Dict[str, Any]) -> float:
    raw = candidate.get("focus_score", candidate.get("combined_score", 0.0))
    try:
        score = float(raw or 0.0)
    except Exception:
        return 0.0
    return float(max(0.0, min(1.0, score)))


def _candidate_activity_score(candidate: Dict[str, Any]) -> float:
    raw = candidate.get("activity_score", candidate.get("speaking_probability", 0.0))
    try:
        score = float(raw or 0.0)
    except Exception:
        return 0.0
    return float(max(0.0, min(1.0, score)))


def _candidate_visual_score(candidate: Dict[str, Any]) -> float:
    score_groups = candidate.get("score_groups", {})
    if isinstance(score_groups, dict) and "visual_score" in score_groups:
        try:
            return float(max(0.0, min(1.0, float(score_groups.get("visual_score", 0.0) or 0.0))))
        except Exception:
            return 0.0
    score_components = candidate.get("score_components", {})
    if not isinstance(score_components, dict):
        score_components = {}
    mouth = float(score_components.get("visual_speaking_prob", score_components.get("mouth_activity", 0.0)) or 0.0)
    face = float(score_components.get("face_confidence", 0.0) or 0.0)
    return float(max(0.0, min(1.0, (0.75 * mouth) + (0.25 * face))))


def _runner_up_focus_score(candidates: List[Dict[str, Any]], best: Optional[Dict[str, Any]]) -> float:
    if best is None:
        return 0.0
    best_id = best.get("track_id")
    scores = [_candidate_focus_score(cand) for cand in candidates if cand.get("track_id") != best_id]
    return max(scores) if scores else 0.0


def _candidate_camera_id(candidate: Dict[str, Any]) -> Optional[str]:
    raw = candidate.get("camera_id")
    if raw:
        return str(raw)
    track_id = str(candidate.get("track_id", "") or "")
    if "-" in track_id and track_id.startswith("cam"):
        return track_id.split("-", 1)[0]
    return None


def _candidate_steering_bearing(candidate: Dict[str, Any]) -> float:
    raw = candidate.get("steering_bearing_deg", candidate.get("bearing_deg", 0.0))
    try:
        bearing = float(raw or 0.0)
    except Exception:
        bearing = 0.0
    return bearing % 360.0


def _unwrap_candidates_payload(payload: CandidatesPayload) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if isinstance(payload, dict):
        candidates = payload.get("candidates", [])
        evidence = payload.get("evidence", {})
        if not isinstance(candidates, list):
            candidates = []
        if not isinstance(evidence, dict):
            evidence = {}
        return candidates, evidence
    if isinstance(payload, list):
        return payload, {}
    return [], {}

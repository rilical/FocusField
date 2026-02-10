"""focusfield.audio.beamform.delay_and_sum

CONTRACT: inline (source: src/focusfield/audio/beamform/delay_and_sum.md)
ROLE: Delay-and-sum beamformer.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
  - Topic: fusion.target_lock  Type: TargetLock
OUTPUTS:
  - Topic: audio.enhanced.beamformed  Type: EnhancedAudio

CONFIG KEYS:
  - audio.beamformer.method: delay_and_sum
  - audio.beamformer.use_last_lock_ms: hold last target
  - audio.beamformer.no_lock_behavior: omni|mute|last_lock
  - audio.beamformer.steering_smoothing_alpha: target bearing smoothing

PERF / TIMING:
  - bounded latency; uses per-block processing

FAILURE MODES:
  - missing lock -> fall back based on no_lock_behavior -> log no_lock
  - missing geometry -> fall back to omni -> log geometry_missing

LOG EVENTS:
  - module=audio.beamform.delay_and_sum, event=no_lock, payload keys=behavior
  - module=audio.beamform.delay_and_sum, event=geometry_missing, payload keys=profile

TESTS:
  - covered indirectly by synthetic beamformer tests
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, Optional, Tuple

import numpy as np

from focusfield.audio.doa.geometry import load_mic_positions
from focusfield.core.clock import now_ns


SPEED_OF_SOUND_M_S = 343.0


def start_delay_and_sum(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    beam_cfg = config.get("audio", {}).get("beamformer", {})
    method = str(beam_cfg.get("method", "delay_and_sum")).lower()
    if method != "delay_and_sum":
        return None

    try:
        positions, channel_order = load_mic_positions(config)
        pos = np.asarray(positions, dtype=np.float32)
        channel_order_arr = np.asarray(channel_order, dtype=np.int64)
    except Exception as exc:  # noqa: BLE001
        logger.emit("error", "audio.beamform.delay_and_sum", "geometry_missing", {"error": str(exc)})
        return None

    sample_rate = int(config.get("audio", {}).get("sample_rate_hz", 48000))
    no_lock_behavior = str(beam_cfg.get("no_lock_behavior", "omni")).lower()
    use_last_lock_ms = float(beam_cfg.get("use_last_lock_ms", 800.0))
    steering_alpha = float(beam_cfg.get("steering_smoothing_alpha", 0.85))
    debug_hz = float(beam_cfg.get("debug_hz", 10.0))
    debug_period_ns = int(1e9 / max(0.1, debug_hz))
    last_target: Optional[Tuple[float, int]] = None
    seq_out = 0
    debug_seq = 0
    last_debug_ns = 0

    q_frames = bus.subscribe("audio.frames")
    q_lock = bus.subscribe("fusion.target_lock")
    last_lock: Optional[Dict[str, Any]] = None

    def _drain_lock() -> None:
        nonlocal last_lock
        try:
            while True:
                last_lock = q_lock.get_nowait()
        except queue.Empty:
            return

    def _run() -> None:
        nonlocal last_target, seq_out, debug_seq, last_debug_ns
        while not stop_event.is_set():
            _drain_lock()
            try:
                frame_msg = q_frames.get(timeout=0.1)
            except queue.Empty:
                continue
            data = frame_msg.get("data")
            if data is None:
                continue
            frame = np.asarray(data)
            if frame.ndim == 1:
                frame = frame[:, None]
            if frame.shape[1] < channel_order_arr.size:
                continue
            x = frame[:, channel_order_arr]
            t_ns = int(frame_msg.get("t_ns", now_ns()))

            target_bearing = _select_target_bearing(last_lock, t_ns, last_target, use_last_lock_ms)
            if target_bearing is None:
                y = _no_lock_output(x, behavior=no_lock_behavior)
                logger.emit("debug", "audio.beamform.delay_and_sum", "no_lock", {"behavior": no_lock_behavior})
                fallback_active = True
                used_target = None
            else:
                if last_target is None:
                    smoothed = target_bearing
                else:
                    smoothed = _smooth_angle(last_target[0], target_bearing, steering_alpha)
                last_target = (smoothed, t_ns)
                y = _delay_and_sum(x, pos, smoothed, sample_rate)
                fallback_active = False
                used_target = float(smoothed)

            if debug_period_ns > 0 and (t_ns - last_debug_ns) >= debug_period_ns:
                debug_seq += 1
                last_debug_ns = t_ns
                bus.publish(
                    "audio.beamformer.debug",
                    {
                        "t_ns": t_ns,
                        "seq": debug_seq,
                        "method": "delay_and_sum",
                        "target_bearing_deg": used_target,
                        "fallback_active": bool(fallback_active),
                    },
                )

            seq_out += 1
            msg = {
                "t_ns": t_ns,
                "seq": seq_out,
                "sample_rate_hz": int(frame_msg.get("sample_rate_hz", sample_rate)),
                "frame_samples": int(frame_msg.get("frame_samples", x.shape[0])),
                "channels": 1,
                "data": y.astype(np.float32),
                "stats": {
                    "rms": float(np.sqrt(np.mean(y**2))) if y.size else 0.0,
                },
            }
            bus.publish("audio.enhanced.beamformed", msg)

    thread = threading.Thread(target=_run, name="beamform-delay-sum", daemon=True)
    thread.start()
    return thread


def _select_target_bearing(
    lock_msg: Optional[Dict[str, Any]],
    t_ns: int,
    last_target: Optional[Tuple[float, int]],
    use_last_lock_ms: float,
) -> Optional[float]:
    if lock_msg is not None:
        state = str(lock_msg.get("state", "NO_LOCK"))
        bearing = lock_msg.get("target_bearing_deg")
        if bearing is not None and state in {"LOCKED", "HANDOFF", "HOLD"}:
            return float(bearing)
    if last_target is None:
        return None
    if use_last_lock_ms <= 0:
        return None
    age_ms = (t_ns - last_target[1]) / 1_000_000.0
    if age_ms <= use_last_lock_ms:
        return float(last_target[0])
    return None


def _no_lock_output(x: np.ndarray, behavior: str) -> np.ndarray:
    behavior = str(behavior or "").lower()
    if behavior == "mute":
        return np.zeros((x.shape[0],), dtype=np.float32)
    # "omni" and "last_lock" both become omni here (last_lock handled upstream)
    return np.mean(x, axis=1).astype(np.float32)


def _delay_and_sum(x: np.ndarray, positions_xy: np.ndarray, bearing_deg: float, sample_rate: int) -> np.ndarray:
    if x.size == 0:
        return np.zeros((0,), dtype=np.float32)
    theta = np.deg2rad(bearing_deg)
    direction = np.array([np.cos(theta), np.sin(theta)], dtype=np.float32)
    delays_s = (positions_xy @ direction) / SPEED_OF_SOUND_M_S
    y = np.zeros((x.shape[0],), dtype=np.float32)
    for ch in range(x.shape[1]):
        shift = int(round(delays_s[ch] * sample_rate))
        y += _shift_samples(x[:, ch], -shift)
    y /= float(max(1, x.shape[1]))
    return y


def _shift_samples(samples: np.ndarray, shift: int) -> np.ndarray:
    if shift == 0:
        return samples.astype(np.float32, copy=False)
    out = np.zeros_like(samples, dtype=np.float32)
    if shift > 0:
        out[shift:] = samples[:-shift]
        return out
    neg = -shift
    out[:-neg] = samples[neg:]
    return out


def _smooth_angle(prev_deg: float, next_deg: float, alpha: float) -> float:
    # Smooth on the unit circle.
    a = np.deg2rad(prev_deg)
    b = np.deg2rad(next_deg)
    va = np.array([np.cos(a), np.sin(a)])
    vb = np.array([np.cos(b), np.sin(b)])
    v = alpha * vb + (1.0 - alpha) * va
    angle = float(np.rad2deg(np.arctan2(v[1], v[0])))
    return angle % 360.0

"""
CONTRACT: inline (source: src/focusfield/audio/capture.md)
ROLE: Produce AudioFrame blocks on audio.frames.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: audio.frames  Type: AudioFrame

CONFIG KEYS:
  - audio.channels: channel count
  - audio.sample_rate_hz: sample rate
  - audio.block_size: frames per block
  - audio.device_profile: mic array profile

PERF / TIMING:
  - fixed cadence at block_size/sample_rate_hz
  - stable seq increments

FAILURE MODES:
  - device disconnect -> reconnect or stop -> log disconnect

LOG EVENTS:
  - module=audio.capture, event=disconnect, payload keys=device_id
  - module=audio.capture, event=underrun, payload keys=frames_dropped

TESTS:
  - tests/contract_tests.md must cover seq monotonicity

CONTRACT DETAILS (inline from src/focusfield/audio/capture.md):
# Audio capture

- Output AudioFrame with fixed block size.
- Maintain monotonic seq and t_ns.
- Expose overflow or underrun events.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, Optional

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None

import numpy as np

from focusfield.core.clock import now_ns
from focusfield.audio.devices import resolve_input_device_index


def start_audio_capture(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    if sd is None:
        logger.emit("error", "audio.capture", "backend_missing", {"backend": "sounddevice"})
        return None

    audio_cfg = config.get("audio", {})
    channels = int(audio_cfg.get("channels", 1))
    sample_rate = int(audio_cfg.get("sample_rate_hz", 48000))
    block_size = int(audio_cfg.get("block_size", 960))
    device_index = resolve_input_device_index(config, logger)
    capture_cfg = audio_cfg.get("capture", {}) if isinstance(audio_cfg, dict) else {}
    if not isinstance(capture_cfg, dict):
        capture_cfg = {}
    allow_mono_fallback = bool(capture_cfg.get("allow_mono_fallback", True))
    portaudio_latency_cfg = capture_cfg.get("portaudio_latency", "high")
    if isinstance(portaudio_latency_cfg, str):
        portaudio_latency = str(portaudio_latency_cfg).strip().lower()
        if portaudio_latency not in {"low", "high"}:
            portaudio_latency = "high"
    else:
        try:
            portaudio_latency = float(portaudio_latency_cfg)
        except Exception:
            portaudio_latency = "high"
        else:
            if portaudio_latency <= 0.0:
                portaudio_latency = "high"
    capture_queue_depth = int(capture_cfg.get("queue_depth", 16) or 16)
    capture_queue_depth = max(4, min(256, capture_queue_depth))
    fail_fast = bool(config.get("runtime", {}).get("fail_fast", True))
    stats_emit_hz = float(capture_cfg.get("stats_emit_hz", 1.0))
    stats_emit_hz = max(0.2, min(10.0, stats_emit_hz))
    stats_period_s = 1.0 / stats_emit_hz
    status_log_interval_s = float(capture_cfg.get("status_log_interval_s", 1.0))
    status_log_interval_s = max(0.25, min(5.0, status_log_interval_s))

    frame_queue: queue.Queue[tuple[int, int, np.ndarray]] = queue.Queue(maxsize=capture_queue_depth)
    stats_lock = threading.Lock()
    stats: Dict[str, int] = {
        "frames_enqueued": 0,
        "frames_published": 0,
        "callback_overflow_drop": 0,
        "status_input_overflow": 0,
        "status_input_overflow_total": 0,
        "status_other": 0,
        "status_other_total": 0,
    }

    status_counts = {"input_overflow": 0}

    def _run() -> None:
        if device_index is None:
            logger.emit(
                "error",
                "audio.capture",
                "device_not_found",
                {"criteria": "input device could not be resolved"},
            )
            if fail_fast:
                stop_event.set()
            return
        stream = _open_stream(logger, channels, sample_rate, block_size, device_index)
        if stream is None:
            return
        with stream:
            logger.emit("info", "audio.capture", "started", {"channels": stream.channels, "sample_rate_hz": sample_rate})
            next_stats_emit = time.time() + stats_period_s
            while not stop_event.is_set():
                try:
                    t_ns, frames, frame = frame_queue.get(timeout=0.05)
                    nonlocal_seq[0] += 1
                    msg = {
                        "t_ns": t_ns,
                        "seq": nonlocal_seq[0],
                        "sample_rate_hz": sample_rate,
                        "frame_samples": frames,
                        "channels": frame.shape[1] if frame.ndim > 1 else 1,
                        "data": frame,
                    }
                    bus.publish("audio.frames", msg)
                    with stats_lock:
                        stats["frames_published"] += 1
                except queue.Empty:
                    pass
                now_s = time.time()
                if now_s >= next_stats_emit:
                    with stats_lock:
                        snapshot = dict(stats)
                    bus.publish(
                        "audio.capture.stats",
                        {
                            "t_ns": now_ns(),
                            "queue_depth": frame_queue.qsize(),
                            **snapshot,
                        },
                    )
                    next_stats_emit = now_s + stats_period_s

    last_status_log_ns = [0]

    def _throttle_status_log() -> bool:
        now_ns = time.perf_counter_ns()
        interval_ns = int(status_log_interval_s * 1_000_000_000)
        if now_ns - last_status_log_ns[0] < interval_ns:
            return False
        last_status_log_ns[0] = now_ns
        return True

    def _callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
        if status:
            status_text = str(status)
            if "input overflow" in status_text.lower():
                with stats_lock:
                    stats["status_input_overflow"] += 1
                    stats["status_input_overflow_total"] += 1
                    status_counts["input_overflow"] += 1
            else:
                with stats_lock:
                    stats["status_other"] += 1
                    stats["status_other_total"] += 1
            if _throttle_status_log():
                logger.emit(
                    "warning",
                    "audio.capture",
                    "underrun",
                    {
                        "status": status_text,
                        "status_input_overflow_count": status_counts["input_overflow"],
                    },
                )
                with stats_lock:
                    status_counts["input_overflow"] = 0
        frame = np.array(indata, copy=True)
        t_ns = now_ns()
        if frame_queue.full():
            try:
                frame_queue.get_nowait()
            except queue.Empty:
                pass
            with stats_lock:
                stats["callback_overflow_drop"] += 1
        try:
            frame_queue.put_nowait((t_ns, int(frames), frame))
            with stats_lock:
                stats["frames_enqueued"] += 1
        except queue.Full:
            with stats_lock:
                stats["callback_overflow_drop"] += 1

    nonlocal_seq = [0]

    def _open_stream(logger_obj, requested_channels, sr, block, device):
        try:
            return sd.InputStream(
                samplerate=sr,
                blocksize=block,
                channels=requested_channels,
                dtype="float32",
                device=device,
                latency=portaudio_latency,
                callback=_callback,
            )
        except Exception as exc:  # noqa: BLE001
            logger_obj.emit(
                "warning",
                "audio.capture",
                "device_error",
                {"error": str(exc), "channels": requested_channels},
            )
            if requested_channels > 1:
                if not allow_mono_fallback:
                    logger_obj.emit(
                        "error",
                        "audio.capture",
                        "mono_fallback_disallowed",
                        {"requested_channels": requested_channels, "device_index": device},
                    )
                    if fail_fast:
                        stop_event.set()
                    return None
                try:
                    return sd.InputStream(
                        samplerate=sr,
                        blocksize=block,
                        channels=1,
                        dtype="float32",
                        device=device,
                        latency=portaudio_latency,
                        callback=_callback,
                    )
                except Exception as exc2:  # noqa: BLE001
                    logger_obj.emit(
                        "error",
                        "audio.capture",
                        "device_error",
                        {"error": str(exc2), "channels": 1},
                    )
            return None

    thread = threading.Thread(target=_run, name="audio-capture", daemon=True)
    thread.start()
    return thread

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

    def _run() -> None:
        stream = _open_stream(logger, channels, sample_rate, block_size, device_index)
        if stream is None:
            return
        with stream:
            logger.emit("info", "audio.capture", "started", {"channels": stream.channels, "sample_rate_hz": sample_rate})
            while not stop_event.is_set():
                time.sleep(0.05)

    def _callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
        if status:
            logger.emit("warning", "audio.capture", "underrun", {"status": str(status)})
        frame = np.array(indata, copy=True)
        t_ns = now_ns()
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

    nonlocal_seq = [0]

    def _open_stream(logger_obj, requested_channels, sr, block, device):
        try:
            return sd.InputStream(
                samplerate=sr,
                blocksize=block,
                channels=requested_channels,
                dtype="float32",
                device=device,
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
                try:
                    return sd.InputStream(
                        samplerate=sr,
                        blocksize=block,
                        channels=1,
                        dtype="float32",
                        device=device,
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

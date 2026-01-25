"""
CONTRACT: inline (source: src/focusfield/audio/vad.md)
ROLE: Voice activity detection from audio.frames.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
OUTPUTS:
  - Topic: audio.vad  Type: AudioVad

CONFIG KEYS:
  - audio.vad.enabled: enable VAD
  - audio.vad.mode: aggressiveness 0..3
  - audio.vad.frame_ms: frame size (10/20/30)
  - audio.vad.min_speech_ratio: speech ratio threshold

PERF / TIMING:
  - per audio frame

FAILURE MODES:
  - backend missing -> log vad_unavailable

LOG EVENTS:
  - module=audio.vad, event=vad_unavailable, payload keys=reason

TESTS:
  - n/a
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, Optional

import numpy as np

from focusfield.core.clock import now_ns

try:
    import webrtcvad
except ImportError:  # pragma: no cover
    webrtcvad = None


class VadProcessor:
    """WebRTC VAD wrapper with buffering."""

    def __init__(self, sample_rate: int, frame_ms: int, mode: int, min_speech_ratio: float) -> None:
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._frame_len = int(sample_rate * frame_ms / 1000)
        self._min_ratio = min_speech_ratio
        self._vad = webrtcvad.Vad(mode) if webrtcvad is not None else None
        self._buffer = np.zeros((0,), dtype=np.float32)
        self._seq = 0

    def process(self, frame: np.ndarray) -> Dict[str, Any]:
        mono = _to_mono(frame)
        self._buffer = np.concatenate([self._buffer, mono])
        speech_frames = 0
        total_frames = 0
        while self._buffer.shape[0] >= self._frame_len:
            chunk = self._buffer[: self._frame_len]
            self._buffer = self._buffer[self._frame_len :]
            speech = self._is_speech(chunk)
            speech_frames += int(bool(speech))
            total_frames += 1
        ratio = speech_frames / total_frames if total_frames else 0.0
        speech_present = ratio >= self._min_ratio
        rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
        self._seq += 1
        return {
            "t_ns": now_ns(),
            "seq": self._seq,
            "speech": speech_present,
            "confidence": ratio,
            "rms": rms,
        }

    def _is_speech(self, samples: np.ndarray) -> bool:
        if self._vad is None:
            return False
        pcm = np.clip(samples, -1.0, 1.0)
        pcm16 = (pcm * 32767).astype(np.int16).tobytes()
        return bool(self._vad.is_speech(pcm16, self._sample_rate))


def start_audio_vad(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    vad_cfg = config.get("audio", {}).get("vad", {})
    if not vad_cfg.get("enabled", True):
        return None
    if webrtcvad is None:
        logger.emit("error", "audio.vad", "vad_unavailable", {"reason": "webrtcvad_missing"})
        return None
    sample_rate = int(config.get("audio", {}).get("sample_rate_hz", 48000))
    frame_ms = int(vad_cfg.get("frame_ms", 20))
    mode = int(vad_cfg.get("mode", 2))
    min_ratio = float(vad_cfg.get("min_speech_ratio", 0.3))
    if sample_rate not in (8000, 16000, 32000, 48000):
        logger.emit("error", "audio.vad", "vad_unavailable", {"reason": "unsupported_sample_rate", "sample_rate": sample_rate})
        return None
    if frame_ms not in (10, 20, 30):
        frame_ms = 20
    processor = VadProcessor(sample_rate, frame_ms, mode, min_ratio)
    q = bus.subscribe("audio.frames")

    def _run() -> None:
        while not stop_event.is_set():
            try:
                frame = q.get(timeout=0.1)
            except queue.Empty:
                continue
            data = frame.get("data")
            if data is None:
                continue
            msg = processor.process(np.asarray(data))
            bus.publish("audio.vad", msg)

    thread = threading.Thread(target=_run, name="audio-vad", daemon=True)
    thread.start()
    return thread


def _to_mono(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 1:
        return frame
    if frame.ndim == 2:
        return np.mean(frame, axis=1)
    return frame.reshape(-1)

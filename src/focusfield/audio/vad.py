"""
CONTRACT: inline (source: src/focusfield/audio/vad.md)
ROLE: Voice activity detection from audio.frames.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
OUTPUTS:
  - Topic: audio.vad  Type: AudioVad

CONFIG KEYS:
  - audio.vad.enabled: enable VAD
  - audio.vad.backend: auto|silero|webrtc
  - audio.vad.mode: aggressiveness 0..3 for WebRTC fallback
  - audio.vad.frame_ms: frame size (10/20/30) for WebRTC fallback
  - audio.vad.min_speech_ratio: WebRTC fallback threshold
  - audio.vad.update_hz: optional publish-rate cap for audio.vad state
  - audio.vad.silero.threshold: posterior threshold for silero backend
"""

from __future__ import annotations

import queue
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from focusfield.core.clock import now_ns

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover
    ort = None

try:
    import webrtcvad
except ImportError:  # pragma: no cover
    webrtcvad = None


class WebRtcVadProcessor:
    """WebRTC VAD wrapper with buffering."""

    def __init__(self, sample_rate: int, frame_ms: int, mode: int, min_speech_ratio: float) -> None:
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._frame_len = int(sample_rate * frame_ms / 1000)
        self._min_ratio = min_speech_ratio
        self._vad = webrtcvad.Vad(mode) if webrtcvad is not None else None
        self._buffer = np.zeros((0,), dtype=np.float32)
        self._seq = 0

    @property
    def backend(self) -> str:
        return "webrtc"

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
        rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
        self._seq += 1
        return {
            "t_ns": now_ns(),
            "seq": self._seq,
            "speech": ratio >= self._min_ratio,
            "confidence": ratio,
            "speech_probability": ratio,
            "backend": self.backend,
            "rms": rms,
        }

    def _is_speech(self, samples: np.ndarray) -> bool:
        if self._vad is None:
            return False
        pcm = np.clip(samples, -1.0, 1.0)
        pcm16 = (pcm * 32767).astype(np.int16).tobytes()
        return bool(self._vad.is_speech(pcm16, self._sample_rate))


class SileroVadProcessor:
    """Silero ONNX wrapper with stateful chunking."""

    def __init__(self, sample_rate: int, threshold: float, model_path: Optional[str] = None) -> None:
        if ort is None:
            raise RuntimeError("onnxruntime is not available")
        self._input_sample_rate = int(sample_rate)
        self._sample_rate = 16000
        self._threshold = float(threshold)
        self._chunk_len = 512
        self._context_len = 64
        self._buffer = np.zeros((0,), dtype=np.float32)
        self._seq = 0
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(_ensure_silero_model(model_path), providers=["CPUExecutionProvider"], sess_options=opts)
        self.reset_states()

    @property
    def backend(self) -> str:
        return "silero"

    def reset_states(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 0), dtype=np.float32)

    def process(self, frame: np.ndarray) -> Dict[str, Any]:
        mono = _to_mono(frame)
        mono_16k = _resample_to_16k(mono, self._input_sample_rate)
        self._buffer = np.concatenate([self._buffer, mono_16k.astype(np.float32, copy=False)])
        probs = []
        while self._buffer.shape[0] >= self._chunk_len:
            chunk = self._buffer[: self._chunk_len]
            self._buffer = self._buffer[self._chunk_len :]
            probs.append(self._predict_chunk(chunk))
        confidence = float(np.mean(probs)) if probs else 0.0
        peak = float(np.max(probs)) if probs else 0.0
        rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
        self._seq += 1
        return {
            "t_ns": now_ns(),
            "seq": self._seq,
            "speech": confidence >= self._threshold,
            "confidence": confidence,
            "speech_probability": peak if peak > confidence else confidence,
            "backend": self.backend,
            "rms": rms,
        }

    def _predict_chunk(self, chunk: np.ndarray) -> float:
        x = chunk.astype(np.float32, copy=False).reshape(1, -1)
        if self._context.shape[1] == 0:
            self._context = np.zeros((1, self._context_len), dtype=np.float32)
        x_cat = np.concatenate([self._context, x], axis=1)
        ort_inputs = {
            "input": x_cat,
            "state": self._state,
            "sr": np.asarray(self._sample_rate, dtype=np.int64),
        }
        outputs = self._session.run(None, ort_inputs)
        self._state = np.asarray(outputs[1], dtype=np.float32)
        self._context = x_cat[:, -self._context_len :]
        return float(np.asarray(outputs[0], dtype=np.float32).reshape(-1)[-1])


def start_audio_vad(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    vad_cfg = config.get("audio", {}).get("vad", {})
    if not vad_cfg.get("enabled", True):
        return None
    sample_rate = int(config.get("audio", {}).get("sample_rate_hz", 48000))
    backend = str(vad_cfg.get("backend", "auto") or "auto").strip().lower()
    frame_ms = int(vad_cfg.get("frame_ms", 20))
    mode = int(vad_cfg.get("mode", 2))
    min_ratio = float(vad_cfg.get("min_speech_ratio", 0.3))
    update_hz = float(vad_cfg.get("update_hz", 0.0) or 0.0)
    silero_cfg = vad_cfg.get("silero", {}) if isinstance(vad_cfg, dict) else {}
    if not isinstance(silero_cfg, dict):
        silero_cfg = {}

    processor: Optional[object] = None
    if backend in {"auto", "silero"}:
        try:
            processor = SileroVadProcessor(
                sample_rate=sample_rate,
                threshold=float(silero_cfg.get("threshold", 0.45)),
                model_path=silero_cfg.get("model_path"),
            )
        except Exception as exc:  # noqa: BLE001
            if backend == "silero":
                logger.emit("warning", "audio.vad", "vad_unavailable", {"reason": "silero_failed", "error": str(exc)})
            elif webrtcvad is None:
                logger.emit("warning", "audio.vad", "vad_unavailable", {"reason": "silero_failed", "error": str(exc)})

    if processor is None:
        if webrtcvad is None:
            logger.emit("error", "audio.vad", "vad_unavailable", {"reason": "webrtcvad_missing"})
            return None
        if sample_rate not in (8000, 16000, 32000, 48000):
            logger.emit("error", "audio.vad", "vad_unavailable", {"reason": "unsupported_sample_rate", "sample_rate": sample_rate})
            return None
        if frame_ms not in (10, 20, 30):
            frame_ms = 20
        processor = WebRtcVadProcessor(sample_rate, frame_ms, mode, min_ratio)

    q = bus.subscribe("audio.frames")

    def _wait_and_drain_latest(q_in: queue.Queue, timeout_s: float = 0.05) -> Optional[Dict[str, Any]]:
        try:
            frame = q_in.get(timeout=timeout_s)
        except queue.Empty:
            return None
        try:
            while True:
                frame = q_in.get_nowait()
        except queue.Empty:
            pass
        return frame

    def _run() -> None:
        idle_cycles = 0
        processed_cycles = 0
        next_stats_emit = time.time() + 1.0
        min_period_ns = int(1e9 / update_hz) if update_hz > 0.0 else 0
        last_publish_ns = 0
        while not stop_event.is_set():
            frame = _wait_and_drain_latest(q)
            if frame is None:
                idle_cycles += 1
            else:
                data = frame.get("data")
                if data is not None:
                    msg = processor.process(np.asarray(data))
                    msg_t_ns = int(msg.get("t_ns", now_ns()))
                    if min_period_ns <= 0 or (msg_t_ns - last_publish_ns) >= min_period_ns:
                        bus.publish("audio.vad", msg)
                        last_publish_ns = msg_t_ns
                    processed_cycles += 1
            now_s = time.time()
            if now_s >= next_stats_emit:
                bus.publish(
                    "runtime.worker_loop",
                    {
                        "t_ns": now_ns(),
                        "module": "audio.vad",
                        "idle_cycles": int(idle_cycles),
                        "processed_cycles": int(processed_cycles),
                    },
                )
                next_stats_emit = now_s + 1.0

    thread = threading.Thread(target=_run, name="audio-vad", daemon=True)
    thread.start()
    return thread


def _to_mono(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 1:
        return frame.astype(np.float32, copy=False)
    if frame.ndim == 2:
        return np.mean(frame, axis=1).astype(np.float32)
    return frame.reshape(-1).astype(np.float32)


def _resample_to_16k(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    if sample_rate == 16000:
        return samples.astype(np.float32, copy=False)
    if sample_rate > 16000 and sample_rate % 16000 == 0:
        step = sample_rate // 16000
        # Apply simple moving-average anti-alias filter before decimation.
        kernel = np.ones(step, dtype=np.float32) / float(step)
        filtered = np.convolve(samples, kernel, mode="same").astype(np.float32)
        return filtered[::step]
    if samples.size == 0:
        return samples.astype(np.float32, copy=False)
    duration_s = samples.shape[0] / float(max(1, sample_rate))
    out_len = max(1, int(round(duration_s * 16000.0)))
    src_x = np.linspace(0.0, 1.0, num=samples.shape[0], endpoint=False)
    dst_x = np.linspace(0.0, 1.0, num=out_len, endpoint=False)
    return np.interp(dst_x, src_x, samples).astype(np.float32)


def _ensure_silero_model(model_path: Optional[str]) -> str:
    if model_path:
        path = Path(model_path)
    else:
        cache_dir = Path.home() / ".cache" / "focusfield"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "silero_vad.onnx"
    if not path.exists():
        url = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to download silero VAD model: {exc}") from exc
    return str(path)

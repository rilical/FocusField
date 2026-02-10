"""focusfield.audio.enhance.denoise

CONTRACT: inline (source: src/focusfield/audio/enhance/denoise.md)
ROLE: Optional denoise stage.

INPUTS:
  - Topic: audio.enhanced.beamformed  Type: EnhancedAudio
OUTPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio

CONFIG KEYS:
  - audio.denoise.enabled: enable denoise
  - audio.denoise.backend: wiener|rnnoise|webrtc
  - audio.denoise.wiener.g_min: minimum gain
  - audio.denoise.wiener.noise_ema_alpha: noise PSD smoothing

PERF / TIMING:
  - per-frame rFFT/irFFT for wiener

FAILURE MODES:
  - backend error -> bypass -> log denoise_failed

LOG EVENTS:
  - module=audio.enhance.denoise, event=denoise_failed, payload keys=backend, error
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

from focusfield.core.clock import now_ns


@dataclass
class _WienerState:
    nfft: int
    noise_psd: Optional[np.ndarray] = None


def start_denoise(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    denoise_cfg = config.get("audio", {}).get("denoise", {})
    if not isinstance(denoise_cfg, dict):
        denoise_cfg = {}
    if not bool(denoise_cfg.get("enabled", False)):
        return None
    backend = str(denoise_cfg.get("backend", "wiener")).lower()
    if backend != "wiener":
        logger.emit("warning", "audio.enhance.denoise", "denoise_failed", {"backend": backend, "error": "unsupported_backend"})
        return None

    wiener_cfg = denoise_cfg.get("wiener", {})
    if not isinstance(wiener_cfg, dict):
        wiener_cfg = {}
    g_min = float(wiener_cfg.get("g_min", 0.05))
    noise_alpha = float(wiener_cfg.get("noise_ema_alpha", 0.98))
    nfft = int(wiener_cfg.get("nfft", config.get("audio", {}).get("block_size", 1024)))
    nfft = max(256, int(2 ** round(np.log2(max(256, nfft)))))
    state = _WienerState(nfft=nfft)

    q_in = bus.subscribe("audio.enhanced.beamformed")
    q_vad = bus.subscribe("audio.vad")
    last_vad: Optional[Dict[str, Any]] = None

    def _drain_latest(q: queue.Queue) -> Optional[Dict[str, Any]]:
        item = None
        try:
            while True:
                item = q.get_nowait()
        except queue.Empty:
            pass
        return item

    def _run() -> None:
        nonlocal last_vad
        seq_out = 0
        while not stop_event.is_set():
            last_vad = _drain_latest(q_vad) or last_vad
            try:
                msg_in = q_in.get(timeout=0.1)
            except queue.Empty:
                continue
            data = msg_in.get("data")
            if data is None:
                continue
            x = np.asarray(data).astype(np.float32)
            if x.ndim != 1:
                x = x.reshape(-1)
            speech = bool(last_vad.get("speech")) if last_vad else False
            y = _wiener_denoise(x, state, speech, noise_alpha, g_min)
            seq_out += 1
            bus.publish(
                "audio.enhanced.final",
                {
                    "t_ns": int(msg_in.get("t_ns", now_ns())),
                    "seq": seq_out,
                    "sample_rate_hz": int(msg_in.get("sample_rate_hz", 48000)),
                    "frame_samples": int(y.shape[0]),
                    "channels": 1,
                    "data": y.astype(np.float32),
                    "stats": {
                        "rms": float(np.sqrt(np.mean(y**2))) if y.size else 0.0,
                    },
                },
            )

    thread = threading.Thread(target=_run, name="audio-denoise", daemon=True)
    thread.start()
    return thread


def _wiener_denoise(
    x: np.ndarray,
    state: _WienerState,
    speech: bool,
    noise_alpha: float,
    g_min: float,
) -> np.ndarray:
    if x.size == 0:
        return x
    nfft = state.nfft
    x_fft = np.fft.rfft(x, n=nfft)
    psd = (np.abs(x_fft) ** 2).astype(np.float32)
    if (not speech) or state.noise_psd is None:
        if state.noise_psd is None:
            state.noise_psd = psd
        else:
            state.noise_psd = noise_alpha * state.noise_psd + (1.0 - noise_alpha) * psd
    noise = state.noise_psd if state.noise_psd is not None else psd
    gain = 1.0 - (noise / (psd + 1e-12))
    gain = np.clip(gain, float(g_min), 1.0).astype(np.float32)
    y_fft = x_fft * gain
    y = np.fft.irfft(y_fft, n=nfft).astype(np.float32)
    return y[: x.shape[0]]


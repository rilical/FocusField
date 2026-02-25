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
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from focusfield.core.clock import now_ns


@dataclass
class _WienerState:
    nfft: int
    noise_psd: Optional[np.ndarray] = None


@dataclass
class _RnnoiseState:
    nfft: int
    noise_psd: Optional[np.ndarray] = None
    gain_ema: Optional[np.ndarray] = None
    model_path: str = ""
    warned_native_missing: bool = False


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

    wiener_cfg = denoise_cfg.get("wiener", {})
    if not isinstance(wiener_cfg, dict):
        wiener_cfg = {}
    g_min = float(wiener_cfg.get("g_min", 0.05))
    noise_alpha = float(wiener_cfg.get("noise_ema_alpha", 0.98))
    nfft = int(wiener_cfg.get("nfft", config.get("audio", {}).get("block_size", 1024)))
    nfft = max(256, int(2 ** round(np.log2(max(256, nfft)))))
    state = _WienerState(nfft=nfft)
    rnnoise_cfg = denoise_cfg.get("rnnoise", {})
    if not isinstance(rnnoise_cfg, dict):
        rnnoise_cfg = {}
    rnnoise_nfft = int(rnnoise_cfg.get("nfft", nfft))
    rnnoise_nfft = max(256, int(2 ** round(np.log2(max(256, rnnoise_nfft)))))
    rnnoise_strength = float(rnnoise_cfg.get("strength", 0.65))
    rnnoise_min_gain = float(rnnoise_cfg.get("min_gain", 0.08))
    rnnoise_noise_alpha = float(rnnoise_cfg.get("noise_ema_alpha", 0.98))
    rnnoise_gain_alpha = float(rnnoise_cfg.get("gain_ema_alpha", 0.85))
    rnnoise_state = _RnnoiseState(
        nfft=rnnoise_nfft,
        model_path=str(rnnoise_cfg.get("model_path", "") or ""),
    )

    hybrid_cfg = denoise_cfg.get("hybrid", {})
    if not isinstance(hybrid_cfg, dict):
        hybrid_cfg = {}
    hybrid_strength = float(hybrid_cfg.get("postfilter_strength", 0.5))
    hybrid_strength = float(min(1.0, max(0.0, hybrid_strength)))

    if backend not in {"wiener", "rnnoise", "hybrid"}:
        logger.emit("warning", "audio.enhance.denoise", "denoise_failed", {"backend": backend, "error": "unsupported_backend"})
        return None
    if rnnoise_state.model_path and not Path(rnnoise_state.model_path).exists():
        logger.emit(
            "warning",
            "audio.enhance.denoise",
            "denoise_failed",
            {"backend": "rnnoise", "error": f"model_path_missing:{rnnoise_state.model_path}"},
        )

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
            y = x
            if backend == "wiener":
                y = _wiener_denoise(y, state, speech, noise_alpha, g_min)
            elif backend == "rnnoise":
                y = _rnnoise_like_denoise(
                    y,
                    rnnoise_state,
                    speech=speech,
                    noise_ema_alpha=rnnoise_noise_alpha,
                    gain_ema_alpha=rnnoise_gain_alpha,
                    strength=rnnoise_strength,
                    min_gain=rnnoise_min_gain,
                )
            else:  # hybrid
                y = _wiener_denoise(y, state, speech, noise_alpha, g_min)
                y_hybrid = _rnnoise_like_denoise(
                    y,
                    rnnoise_state,
                    speech=speech,
                    noise_ema_alpha=rnnoise_noise_alpha,
                    gain_ema_alpha=rnnoise_gain_alpha,
                    strength=rnnoise_strength,
                    min_gain=rnnoise_min_gain,
                )
                y = (1.0 - hybrid_strength) * y + hybrid_strength * y_hybrid
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


def _rnnoise_like_denoise(
    x: np.ndarray,
    state: _RnnoiseState,
    speech: bool,
    noise_ema_alpha: float,
    gain_ema_alpha: float,
    strength: float,
    min_gain: float,
) -> np.ndarray:
    """RNNoise-like spectral suppression (dependency-light fallback)."""
    if x.size == 0:
        return x

    nfft = state.nfft
    x_fft = np.fft.rfft(x, n=nfft)
    mag = np.abs(x_fft).astype(np.float32)
    psd = (mag**2).astype(np.float32)

    if state.noise_psd is None:
        state.noise_psd = psd.copy()
    if (not speech) or state.noise_psd is None:
        state.noise_psd = noise_ema_alpha * state.noise_psd + (1.0 - noise_ema_alpha) * psd
    else:
        state.noise_psd = np.minimum(state.noise_psd, psd * 1.25)

    noise = np.maximum(state.noise_psd, 1e-12)
    snr = np.maximum(psd - noise, 0.0) / noise
    # Smooth non-linear mapping to [min_gain, 1], stronger suppression via `strength`.
    logistic = 1.0 / (1.0 + np.exp(-(snr - 1.0)))
    target_gain = min_gain + (1.0 - min_gain) * np.power(logistic, max(0.1, 1.0 + 2.0 * strength))
    target_gain = np.clip(target_gain, min_gain, 1.0).astype(np.float32)

    if state.gain_ema is None:
        state.gain_ema = target_gain
    state.gain_ema = gain_ema_alpha * state.gain_ema + (1.0 - gain_ema_alpha) * target_gain

    y_fft = x_fft * state.gain_ema.astype(np.complex64)
    y = np.fft.irfft(y_fft, n=nfft).astype(np.float32)
    return y[: x.shape[0]]

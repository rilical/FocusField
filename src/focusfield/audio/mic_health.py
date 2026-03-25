"""
CONTRACT: inline
ROLE: Estimate per-channel microphone health from audio.frames.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
OUTPUTS:
  - Topic: audio.mic_health  Type: MicHealth

CONFIG KEYS:
  - audio.mic_health.enabled: enable mic health worker
  - audio.mic_health.score_ema_alpha: temporal smoothing for scores
  - audio.mic_health.noise_ema_alpha: temporal smoothing for noise floor
  - audio.mic_health.dead_rms_threshold: hard mute threshold
  - audio.mic_health.max_clip_fraction: clipping threshold
  - audio.mic_health.max_dc_offset: DC bias threshold
  - audio.mic_health.max_dropout_fraction: dropout threshold
  - audio.mic_health.min_coherence: minimum acceptable coherence
  - audio.mic_health.max_drift_samples: drift threshold

PERF / TIMING:
  - per audio frame; lightweight rolling statistics

FAILURE MODES:
  - invalid frame -> skip publish

LOG EVENTS:
  - module=audio.mic_health, event=analysis_failed, payload keys=error
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from focusfield.audio.fft_backend import rfft
from focusfield.audio.sync.drift_check import _estimate_offset_samples
from focusfield.core.clock import now_ns


@dataclass
class _MicHealthState:
    noise_floor: Optional[np.ndarray] = None
    score_ema: Optional[np.ndarray] = None
    trust_ema: Optional[np.ndarray] = None
    drift_samples: Optional[np.ndarray] = None
    seq: int = 0


class MicHealthAnalyzer:
    """Estimate per-channel health from rolling audio statistics."""

    def __init__(self, config: Dict[str, Any]) -> None:
        audio_cfg = config.get("audio", {})
        health_cfg = audio_cfg.get("mic_health", {}) if isinstance(audio_cfg, dict) else {}
        if not isinstance(health_cfg, dict):
            health_cfg = {}
        self._score_alpha = float(health_cfg.get("score_ema_alpha", 0.8))
        self._noise_alpha = float(health_cfg.get("noise_ema_alpha", 0.97))
        self._dead_rms_threshold = float(health_cfg.get("dead_rms_threshold", 1e-5))
        self._max_clip_fraction = float(health_cfg.get("max_clip_fraction", 0.01))
        self._max_dc_offset = float(health_cfg.get("max_dc_offset", 0.03))
        self._max_dropout_fraction = float(health_cfg.get("max_dropout_fraction", 0.98))
        self._min_coherence = float(health_cfg.get("min_coherence", 0.12))
        self._max_drift_samples = int(health_cfg.get("max_drift_samples", 8))
        self._min_active_score = float(health_cfg.get("min_active_score", 0.35))
        self._drift_every_n = max(1, int(health_cfg.get("drift_every_n", 6)))
        self._state = _MicHealthState()

    def update(self, frame_msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        data = frame_msg.get("data")
        if data is None:
            return None
        x = np.asarray(data, dtype=np.float32)
        if x.ndim == 1:
            x = x[:, None]
        if x.ndim != 2 or x.shape[1] == 0:
            return None

        channels = int(x.shape[1])
        rms = np.sqrt(np.mean(x**2, axis=0)).astype(np.float32)
        clip_fraction = np.mean(np.abs(x) >= 0.999, axis=0).astype(np.float32)
        dc_offset = np.abs(np.mean(x, axis=0)).astype(np.float32)
        dropout_fraction = np.mean(np.abs(x) <= self._dead_rms_threshold, axis=0).astype(np.float32)
        coherence = self._estimate_coherence(frame_msg, x)
        drift = self._estimate_drift(x)
        noise_floor = self._update_noise_floor(rms)
        snr_db = 20.0 * np.log10((rms + 1e-8) / (noise_floor + 1e-8))

        score_raw = self._score_channels(
            rms=rms,
            clip_fraction=clip_fraction,
            dc_offset=dc_offset,
            dropout_fraction=dropout_fraction,
            coherence=coherence,
            drift=drift,
            snr_db=snr_db,
        )
        trust_raw = self._trust_channels(coherence=coherence, drift=drift, rms=rms)

        if self._state.score_ema is None or self._state.score_ema.shape[0] != channels:
            self._state.score_ema = score_raw.copy()
        else:
            self._state.score_ema = self._score_alpha * self._state.score_ema + (1.0 - self._score_alpha) * score_raw

        if self._state.trust_ema is None or self._state.trust_ema.shape[0] != channels:
            self._state.trust_ema = trust_raw.copy()
        else:
            self._state.trust_ema = self._score_alpha * self._state.trust_ema + (1.0 - self._score_alpha) * trust_raw

        self._state.seq += 1
        scores = np.clip(self._state.score_ema, 0.0, 1.0).astype(np.float32)
        trust = np.clip(self._state.trust_ema, 0.0, 1.0).astype(np.float32)
        active_channels = [int(idx) for idx, score in enumerate(scores) if float(score) >= self._min_active_score]

        channels_out: List[Dict[str, Any]] = []
        for idx in range(channels):
            bad_reason = _bad_reason(
                rms=float(rms[idx]),
                clip_fraction=float(clip_fraction[idx]),
                dc_offset=float(dc_offset[idx]),
                dropout_fraction=float(dropout_fraction[idx]),
                coherence=float(coherence[idx]),
                drift=int(drift[idx]),
                dead_rms_threshold=self._dead_rms_threshold,
                max_clip_fraction=self._max_clip_fraction,
                max_dc_offset=self._max_dc_offset,
                max_dropout_fraction=self._max_dropout_fraction,
                min_coherence=self._min_coherence,
                max_drift_samples=self._max_drift_samples,
            )
            channels_out.append(
                {
                    "channel": idx,
                    "score": float(scores[idx]),
                    "trust": float(trust[idx]),
                    "rms": float(rms[idx]),
                    "noise_floor": float(noise_floor[idx]),
                    "snr_db": float(snr_db[idx]),
                    "clip_fraction": float(clip_fraction[idx]),
                    "dc_offset": float(dc_offset[idx]),
                    "dropout_fraction": float(dropout_fraction[idx]),
                    "coherence": float(coherence[idx]),
                    "drift_samples": int(drift[idx]),
                    "bad_reason": bad_reason,
                }
            )

        return {
            "t_ns": int(frame_msg.get("t_ns", now_ns())),
            "seq": int(self._state.seq),
            "channels": channels_out,
            "active_channels": active_channels,
            "mean_score": float(np.mean(scores)) if scores.size else 0.0,
            "mean_trust": float(np.mean(trust)) if trust.size else 0.0,
        }

    def _estimate_coherence(self, frame_msg: Dict[str, Any], x: np.ndarray) -> np.ndarray:
        x_fft = frame_msg.get("data_fft")
        if x_fft is None:
            spectrum = rfft(x, axis=0).astype(np.complex64)
        else:
            spectrum = np.asarray(x_fft).astype(np.complex64)
        if spectrum.ndim != 2 or spectrum.shape[1] != x.shape[1]:
            spectrum = rfft(x, axis=0).astype(np.complex64)
        ref_idx = _reference_channel(x.shape[1])
        ref = spectrum[:, ref_idx]
        coherence = np.ones((x.shape[1],), dtype=np.float32)
        for ch in range(x.shape[1]):
            if ch == ref_idx:
                continue
            cross = spectrum[:, ch] * np.conj(ref)
            denom = np.maximum(np.abs(cross), 1e-12)
            coherence[ch] = float(np.abs(np.mean(cross / denom)))
        return np.clip(coherence, 0.0, 1.0).astype(np.float32)

    def _estimate_drift(self, x: np.ndarray) -> np.ndarray:
        channels = x.shape[1]
        if channels <= 1:
            return np.zeros((channels,), dtype=np.int32)
        if self._state.seq % self._drift_every_n != 0 and self._state.drift_samples is not None:
            return self._state.drift_samples
        ref_idx = _reference_channel(channels)
        ref = x[:, ref_idx].astype(np.float32)
        drift = np.zeros((channels,), dtype=np.int32)
        for ch in range(channels):
            if ch == ref_idx:
                continue
            drift[ch] = int(_estimate_offset_samples(ref, x[:, ch].astype(np.float32)))
        self._state.drift_samples = drift
        return drift

    def _update_noise_floor(self, rms: np.ndarray) -> np.ndarray:
        if self._state.noise_floor is None or self._state.noise_floor.shape[0] != rms.shape[0]:
            self._state.noise_floor = np.maximum(rms * 0.75, self._dead_rms_threshold).astype(np.float32)
            return self._state.noise_floor
        prev = self._state.noise_floor
        lower = np.minimum(prev, rms)
        rising = self._noise_alpha * prev + (1.0 - self._noise_alpha) * rms
        self._state.noise_floor = np.where(rms <= prev, lower, rising).astype(np.float32)
        self._state.noise_floor = np.maximum(self._state.noise_floor, self._dead_rms_threshold).astype(np.float32)
        return self._state.noise_floor

    def _score_channels(
        self,
        rms: np.ndarray,
        clip_fraction: np.ndarray,
        dc_offset: np.ndarray,
        dropout_fraction: np.ndarray,
        coherence: np.ndarray,
        drift: np.ndarray,
        snr_db: np.ndarray,
    ) -> np.ndarray:
        rms_score = np.clip((rms - self._dead_rms_threshold) / max(self._dead_rms_threshold * 10.0, 1e-6), 0.0, 1.0)
        clip_score = np.clip(1.0 - (clip_fraction / max(self._max_clip_fraction, 1e-6)), 0.0, 1.0)
        dc_score = np.clip(1.0 - (dc_offset / max(self._max_dc_offset, 1e-6)), 0.0, 1.0)
        dropout_score = np.clip(1.0 - (dropout_fraction / max(self._max_dropout_fraction, 1e-6)), 0.0, 1.0)
        coherence_score = np.clip((coherence - self._min_coherence) / max(1e-6, 1.0 - self._min_coherence), 0.0, 1.0)
        drift_score = np.clip(1.0 - (np.abs(drift).astype(np.float32) / max(1.0, float(self._max_drift_samples))), 0.0, 1.0)
        snr_score = np.clip((snr_db + 3.0) / 21.0, 0.0, 1.0)
        score = (
            0.14 * rms_score
            + 0.16 * clip_score
            + 0.10 * dc_score
            + 0.16 * dropout_score
            + 0.18 * coherence_score
            + 0.10 * drift_score
            + 0.16 * snr_score
        )
        score = np.where(rms < self._dead_rms_threshold, 0.0, score)
        score = np.where(clip_fraction >= self._max_clip_fraction, 0.0, score)
        score = np.where(dropout_fraction >= self._max_dropout_fraction, 0.0, score)
        return np.clip(score, 0.0, 1.0).astype(np.float32)

    @staticmethod
    def _trust_channels(coherence: np.ndarray, drift: np.ndarray, rms: np.ndarray) -> np.ndarray:
        drift_score = np.clip(1.0 - (np.abs(drift).astype(np.float32) / 12.0), 0.0, 1.0)
        rms_score = np.clip(rms / np.maximum(np.max(rms), 1e-6), 0.0, 1.0)
        trust = 0.55 * coherence + 0.25 * drift_score + 0.20 * rms_score
        return np.clip(trust, 0.0, 1.0).astype(np.float32)


def start_audio_mic_health(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    audio_cfg = config.get("audio", {})
    health_cfg = audio_cfg.get("mic_health", {}) if isinstance(audio_cfg, dict) else {}
    if not isinstance(health_cfg, dict):
        health_cfg = {}
    if not bool(health_cfg.get("enabled", True)):
        return None
    analyzer = MicHealthAnalyzer(config)
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
        while not stop_event.is_set():
            frame = _wait_and_drain_latest(q)
            if frame is None:
                idle_cycles += 1
            else:
                try:
                    msg = analyzer.update(frame)
                except Exception as exc:  # noqa: BLE001
                    logger.emit("warning", "audio.mic_health", "analysis_failed", {"error": str(exc)})
                    msg = None
                if msg is not None:
                    bus.publish("audio.mic_health", msg)
                    processed_cycles += 1
            now_s = time.time()
            if now_s >= next_stats_emit:
                bus.publish(
                    "runtime.worker_loop",
                    {
                        "t_ns": now_ns(),
                        "module": "audio.mic_health",
                        "idle_cycles": int(idle_cycles),
                        "processed_cycles": int(processed_cycles),
                    },
                )
                next_stats_emit = now_s + 1.0

    thread = threading.Thread(target=_run, name="audio-mic-health", daemon=True)
    thread.start()
    return thread


def _reference_channel(channels: int) -> int:
    if channels <= 1:
        return 0
    center = channels - 1
    if center >= 0:
        return center
    return 0


def channel_health_vectors(
    mic_health: Optional[Dict[str, Any]],
    channels: int,
    channel_order: Optional[List[int] | np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Extract per-channel score and trust arrays from a mic_health message.

    Shared by SRP-PHAT and MVDR to avoid duplication.
    """
    scores = np.ones((channels,), dtype=np.float32)
    trust = np.ones((channels,), dtype=np.float32)
    if not isinstance(mic_health, dict):
        return scores, trust
    entries = mic_health.get("channels")
    if not isinstance(entries, list):
        return scores, trust
    order_map: Optional[List[int]] = None
    if channel_order is not None:
        order_map = [int(ch) for ch in channel_order][:channels]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        idx = int(entry.get("channel", -1) or -1)
        if order_map is not None:
            try:
                idx = order_map.index(idx)
            except ValueError:
                continue
        if idx < 0 or idx >= channels:
            continue
        score_value = entry.get("score")
        trust_value = entry.get("trust")
        bad_reason = str(entry.get("bad_reason", "") or "")
        if score_value is not None:
            scores[idx] = float(score_value)
        if trust_value is not None:
            trust[idx] = float(trust_value)
        if "dead" in bad_reason or "dropout" in bad_reason:
            scores[idx] = 0.0
            trust[idx] = 0.0
    return np.clip(scores, 0.0, 1.0).astype(np.float32), np.clip(trust, 0.0, 1.0).astype(np.float32)


def _bad_reason(
    rms: float,
    clip_fraction: float,
    dc_offset: float,
    dropout_fraction: float,
    coherence: float,
    drift: int,
    dead_rms_threshold: float,
    max_clip_fraction: float,
    max_dc_offset: float,
    max_dropout_fraction: float,
    min_coherence: float,
    max_drift_samples: int,
) -> str:
    reasons: List[str] = []
    if rms < dead_rms_threshold:
        reasons.append("dead")
    if clip_fraction >= max_clip_fraction:
        reasons.append("clipping")
    if dc_offset >= max_dc_offset:
        reasons.append("dc_offset")
    if dropout_fraction >= max_dropout_fraction:
        reasons.append("dropout")
    if coherence < min_coherence:
        reasons.append("low_coherence")
    if abs(drift) > max_drift_samples:
        reasons.append("drift")
    return ",".join(reasons)

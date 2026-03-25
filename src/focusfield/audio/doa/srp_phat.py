"""
CONTRACT: inline (source: src/focusfield/audio/doa/srp_phat.md)
ROLE: SRP-PHAT heatmap generation.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
OUTPUTS:
  - Topic: audio.doa_heatmap  Type: DoaHeatmap

CONFIG KEYS:
  - audio.doa.bins: number of azimuth bins
  - audio.doa.update_hz: heatmap update rate
  - audio.doa.freq_band_hz: optional band
  - audio.doa.smoothing_alpha: temporal smoothing
  - audio.doa.top_k_peaks: peak count

PERF / TIMING:
  - update at configured rate; UI target >= 10 Hz

FAILURE MODES:
  - no speech or low energy -> low confidence heatmap -> log doa_low_confidence

LOG EVENTS:
  - module=audio.doa.srp_phat, event=doa_low_confidence, payload keys=confidence

TESTS:
  - tests/audio_doa_sanity.md must cover heatmap peaks

CONTRACT DETAILS (inline from src/focusfield/audio/doa/srp_phat.md):
# SRP-PHAT heatmap

## Angle bins

- Define bin size in degrees and total bin count.
- Angles wrap to [0, 360).

## Update rate

- Heatmap update_hz is configurable.
- Output DoaHeatmap at the configured rate.

## Peak finding

- Top-K peak extraction.
- Peak list includes angle_deg and score.

## Smoothing

- Optional temporal smoothing of heatmap.
- Smoothing window and alpha set in config.

## Confidence

- Confidence computed from peak-to-mean ratio or top-K spread.
- Normalized to 0..1.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from focusfield.audio.fft_backend import rfft, rfftfreq
from focusfield.audio.doa.geometry import load_mic_positions
from focusfield.audio.mic_health import channel_health_vectors
from focusfield.core.clock import now_ns


SPEED_OF_SOUND_M_S = 343.0


class SrpPhatDoa:
    """SRP-PHAT DOA estimator producing a 0..360 heatmap."""

    def __init__(self, config: Dict[str, Any]) -> None:
        audio_cfg = config.get("audio", {})
        doa_cfg = audio_cfg.get("doa", {})
        self._sample_rate = int(audio_cfg.get("sample_rate_hz", 48000))
        self._block_size = int(audio_cfg.get("block_size", 1024))
        self._bins = int(doa_cfg.get("bins", 72))
        self._update_hz = float(doa_cfg.get("update_hz", 10.0))
        self._smoothing_alpha = float(doa_cfg.get("smoothing_alpha", 0.3))
        self._top_k = int(doa_cfg.get("top_k_peaks", 3))
        self._energy_threshold = float(doa_cfg.get("energy_threshold", 1e-4))
        self._min_confidence = float(doa_cfg.get("min_confidence", 0.2))
        self._continuity_weight = float(doa_cfg.get("continuity_weight", 0.15))
        self._continuity_sigma_bins = float(doa_cfg.get("continuity_sigma_bins", max(1.5, self._bins / 18.0)))
        self._max_jump_deg = float(doa_cfg.get("max_jump_deg", 60.0))
        self._outlier_hold_confidence = float(doa_cfg.get("outlier_hold_confidence", 0.55))
        self._pair_weight_alpha = float(doa_cfg.get("pair_weight_alpha", 0.90))
        self._angles_deg = np.linspace(0.0, 360.0, num=self._bins, endpoint=False)

        positions, channel_order = load_mic_positions(config)
        self._channel_order = channel_order
        self._positions = np.array(positions, dtype=np.float32)
        if self._positions.shape[0] < 2:
            raise ValueError("SRP-PHAT requires at least 2 microphones")
        self._pairs = _build_pairs(self._positions.shape[0])
        self._pair_i = np.asarray([pair[0] for pair in self._pairs], dtype=np.int64)
        self._pair_j = np.asarray([pair[1] for pair in self._pairs], dtype=np.int64)
        self._pair_weights = np.ones((len(self._pairs),), dtype=np.float32)

        freqs = rfftfreq(self._block_size, d=1.0 / self._sample_rate)
        freq_band = doa_cfg.get("freq_band_hz")
        if isinstance(freq_band, (list, tuple)) and len(freq_band) == 2:
            f_lo = float(freq_band[0])
            f_hi = float(freq_band[1])
            mask = (freqs >= f_lo) & (freqs <= f_hi)
        else:
            f_lo = float(doa_cfg.get("freq_low_hz", 200.0))
            f_hi = float(doa_cfg.get("freq_high_hz", 4500.0))
            mask = (freqs >= f_lo) & (freqs <= f_hi)
        self._freqs = freqs[mask]
        self._freq_idx = np.where(mask)[0]

        self._phase_tables = _precompute_phase_tables(
            self._positions,
            self._pairs,
            self._angles_deg,
            self._freqs,
        )
        self._prev = np.zeros(self._bins, dtype=np.float32)
        self._prev_peak_idx: Optional[int] = None
        self._last_update_ns = 0
        self._seq = 0

    def update(self, frame_msg: Dict[str, Any], mic_health: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        t_ns = int(frame_msg.get("t_ns", now_ns()))
        if self._update_hz > 0:
            min_period_ns = int(1e9 / self._update_hz)
            if self._last_update_ns and (t_ns - self._last_update_ns) < min_period_ns:
                return None
        data = frame_msg.get("data")
        if data is None:
            return None
        frame = np.asarray(data)
        if frame.ndim == 1:
            frame = frame[:, None]
        if frame.shape[1] < len(self._channel_order):
            return None
        frame = frame[:, self._channel_order]
        frame_fft = frame_msg.get("data_fft")
        if frame_fft is not None:
            spectrum_full = np.asarray(frame_fft)
            if spectrum_full.ndim == 2 and spectrum_full.shape[1] >= len(self._channel_order):
                spectrum = spectrum_full[:, self._channel_order]
                spectrum = spectrum[self._freq_idx, :].astype(np.complex64, copy=False)
            else:
                spectrum = rfft(frame, axis=0)[self._freq_idx, :].astype(np.complex64)
        else:
            spectrum = rfft(frame, axis=0)[self._freq_idx, :].astype(np.complex64)
        rms = float(np.sqrt(np.mean(frame**2)))
        if rms < self._energy_threshold:
            heatmap = np.zeros(self._bins, dtype=np.float32)
            msg = self._build_msg(t_ns, heatmap, confidence=0.0)
            return msg
        channel_scores, channel_trust = channel_health_vectors(
            mic_health,
            frame.shape[1],
            channel_order=self._channel_order,
        )
        cross = (spectrum[:, self._pair_i] * np.conj(spectrum[:, self._pair_j])).T.astype(np.complex64)
        cross = cross / np.maximum(np.abs(cross), 1e-12)
        coherence = np.abs(np.mean(cross, axis=1)).astype(np.float32)
        pair_reliability = np.minimum(channel_scores[self._pair_i], channel_scores[self._pair_j]) * np.sqrt(
            channel_trust[self._pair_i] * channel_trust[self._pair_j]
        )
        target_pair_weights = np.clip(coherence * pair_reliability, 0.05, 1.0).astype(np.float32)
        self._pair_weights = (
            self._pair_weight_alpha * self._pair_weights + (1.0 - self._pair_weight_alpha) * target_pair_weights
        ).astype(np.float32)
        scores = np.real(
            np.einsum(
                "paf,pf->a",
                self._phase_tables,
                cross * self._pair_weights[:, None].astype(np.complex64),
                optimize=True,
            )
        ).astype(np.float32)

        min_val = float(scores.min()) if scores.size else 0.0
        if min_val < 0:
            scores = scores - min_val
        max_val = float(scores.max()) if scores.size else 0.0
        if max_val > 0:
            scores = scores / max_val
        scores = self._smoothing_alpha * scores + (1.0 - self._smoothing_alpha) * self._prev
        scores = self._apply_peak_continuity(scores)

        current_peak_idx = int(np.argmax(scores)) if scores.size else None
        confidence = _confidence(scores)
        if self._prev_peak_idx is not None and current_peak_idx is not None:
            jump_deg = abs(_wrap_deg(float(self._angles_deg[current_peak_idx] - self._angles_deg[self._prev_peak_idx])))
            if jump_deg > self._max_jump_deg and confidence < self._outlier_hold_confidence:
                # Reject low-confidence abrupt jumps to reduce steering jitter.
                scores = 0.75 * self._prev + 0.25 * scores
                current_peak_idx = int(np.argmax(scores)) if scores.size else None
                confidence = _confidence(scores)

        self._prev = scores
        self._prev_peak_idx = current_peak_idx
        self._last_update_ns = t_ns
        msg = self._build_msg(t_ns, scores, confidence=confidence)
        msg["pair_weights_mean"] = float(np.mean(self._pair_weights)) if self._pair_weights.size else 0.0
        msg["mic_health_mean"] = float(np.mean(channel_scores)) if channel_scores.size else 0.0
        return msg

    def _build_msg(self, t_ns: int, scores: np.ndarray, confidence: float) -> Dict[str, Any]:
        peaks = _top_peaks(scores, self._angles_deg, self._top_k)
        self._seq += 1
        return {
            "t_ns": t_ns,
            "seq": self._seq,
            "method": "SRP-PHAT",
            "bins": int(self._bins),
            "bin_size_deg": float(360.0 / self._bins),
            "heatmap": scores.tolist(),
            "peaks": peaks,
            "confidence": float(confidence),
        }

    @property
    def min_confidence(self) -> float:
        return self._min_confidence

    def _apply_peak_continuity(self, scores: np.ndarray) -> np.ndarray:
        if self._prev_peak_idx is None or scores.size == 0:
            return scores
        bins = np.arange(scores.size, dtype=np.float32)
        dist = np.abs(bins - float(self._prev_peak_idx))
        dist = np.minimum(dist, float(scores.size) - dist)
        kernel = np.exp(-0.5 * (dist / max(1e-3, self._continuity_sigma_bins)) ** 2).astype(np.float32)
        boosted = scores + self._continuity_weight * kernel
        peak = float(np.max(boosted)) if boosted.size else 0.0
        if peak > 0:
            boosted = boosted / peak
        return boosted


def start_srp_phat(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    doa_cfg = config.get("audio", {}).get("doa", {})
    method = str(doa_cfg.get("method", "srp_phat")).lower()
    if method != "srp_phat":
        return None
    try:
        estimator = SrpPhatDoa(config)
    except Exception as exc:  # noqa: BLE001
        logger.emit("error", "audio.doa.srp_phat", "doa_failed", {"error": str(exc)})
        return None
    q = bus.subscribe("audio.frames")
    q_mic_health = bus.subscribe("audio.mic_health")

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
        last_mic_health: Optional[Dict[str, Any]] = None
        idle_cycles = 0
        processed_cycles = 0
        next_stats_emit = time.time() + 1.0
        while not stop_event.is_set():
            try:
                while True:
                    last_mic_health = q_mic_health.get_nowait()
            except queue.Empty:
                pass
            frame = _wait_and_drain_latest(q)
            if frame is None:
                idle_cycles += 1
            else:
                msg = estimator.update(frame, last_mic_health)
                if msg is not None:
                    if msg.get("confidence", 0.0) < estimator.min_confidence:
                        logger.emit("debug", "audio.doa.srp_phat", "doa_low_confidence", {"confidence": msg.get("confidence", 0.0)})
                    bus.publish("audio.doa_heatmap", msg)
                    processed_cycles += 1
            now_s = time.time()
            if now_s >= next_stats_emit:
                bus.publish(
                    "runtime.worker_loop",
                    {
                        "t_ns": now_ns(),
                        "module": "audio.doa.srp_phat",
                        "idle_cycles": int(idle_cycles),
                        "processed_cycles": int(processed_cycles),
                    },
                )
                next_stats_emit = now_s + 1.0

    thread = threading.Thread(target=_run, name="doa-srp-phat", daemon=True)
    thread.start()
    return thread


def _build_pairs(count: int) -> List[Tuple[int, int]]:
    pairs: List[Tuple[int, int]] = []
    for i in range(count):
        for j in range(i + 1, count):
            pairs.append((i, j))
    return pairs


def _precompute_phase_tables(
    positions: np.ndarray,
    pairs: List[Tuple[int, int]],
    angles_deg: np.ndarray,
    freqs_hz: np.ndarray,
) -> np.ndarray:
    tables: List[np.ndarray] = []
    angles_rad = np.deg2rad(angles_deg)
    dir_vectors = np.stack([np.cos(angles_rad), np.sin(angles_rad)], axis=1)
    for (i, j) in pairs:
        diff = positions[i] - positions[j]
        delays = (dir_vectors @ diff) / SPEED_OF_SOUND_M_S
        phase = np.exp(1j * 2.0 * math.pi * delays[:, None] * freqs_hz[None, :])
        tables.append(phase.astype(np.complex64))
    return np.stack(tables, axis=0)


def _top_peaks(scores: np.ndarray, angles_deg: np.ndarray, top_k: int) -> List[Dict[str, float]]:
    if scores.size == 0:
        return []
    count = max(1, top_k)
    indices = np.argsort(scores)[::-1][:count]
    peaks: List[Dict[str, float]] = []
    for idx in indices:
        peaks.append({"angle_deg": float(angles_deg[idx]), "score": float(scores[idx])})
    return peaks


def _confidence(scores: np.ndarray) -> float:
    if scores.size == 0:
        return 0.0
    peak = float(scores.max())
    mean = float(scores.mean())
    if peak <= 1e-6:
        return 0.0
    confidence = (peak - mean) / max(peak, 1e-6)
    return float(max(0.0, min(1.0, confidence)))


def _wrap_deg(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0

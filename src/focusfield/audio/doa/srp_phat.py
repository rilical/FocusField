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
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from focusfield.audio.doa.geometry import load_mic_positions
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
        self._angles_deg = np.linspace(0.0, 360.0, num=self._bins, endpoint=False)

        positions, channel_order = load_mic_positions(config)
        self._channel_order = channel_order
        self._positions = np.array(positions, dtype=np.float32)
        if self._positions.shape[0] < 2:
            raise ValueError("SRP-PHAT requires at least 2 microphones")
        self._pairs = _build_pairs(self._positions.shape[0])

        freqs = np.fft.rfftfreq(self._block_size, d=1.0 / self._sample_rate)
        freq_band = doa_cfg.get("freq_band_hz")
        if isinstance(freq_band, (list, tuple)) and len(freq_band) == 2:
            f_lo = float(freq_band[0])
            f_hi = float(freq_band[1])
            mask = (freqs >= f_lo) & (freqs <= f_hi)
        else:
            mask = np.ones_like(freqs, dtype=bool)
        self._freqs = freqs[mask]
        self._freq_idx = np.where(mask)[0]

        self._phase_tables = _precompute_phase_tables(
            self._positions,
            self._pairs,
            self._angles_deg,
            self._freqs,
        )
        self._prev = np.zeros(self._bins, dtype=np.float32)
        self._last_update_ns = 0
        self._seq = 0

    def update(self, frame_msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
        rms = float(np.sqrt(np.mean(frame**2)))
        if rms < self._energy_threshold:
            heatmap = np.zeros(self._bins, dtype=np.float32)
            msg = self._build_msg(t_ns, heatmap, confidence=0.0)
            return msg

        spectrum = np.fft.rfft(frame, axis=0)
        spectrum = spectrum[self._freq_idx, :]
        scores = np.zeros(self._bins, dtype=np.float32)

        for pair_idx, (i, j) in enumerate(self._pairs):
            cross = spectrum[:, i] * np.conj(spectrum[:, j])
            denom = np.abs(cross)
            cross = cross / np.maximum(denom, 1e-12)
            table = self._phase_tables[pair_idx]
            scores += np.real(table @ cross)

        min_val = float(scores.min()) if scores.size else 0.0
        if min_val < 0:
            scores = scores - min_val
        max_val = float(scores.max()) if scores.size else 0.0
        if max_val > 0:
            scores = scores / max_val
        scores = self._smoothing_alpha * scores + (1.0 - self._smoothing_alpha) * self._prev
        self._prev = scores
        confidence = _confidence(scores)
        self._last_update_ns = t_ns
        msg = self._build_msg(t_ns, scores, confidence=confidence)
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

    def _run() -> None:
        while not stop_event.is_set():
            try:
                frame = q.get(timeout=0.1)
            except queue.Empty:
                continue
            msg = estimator.update(frame)
            if msg is None:
                continue
            if msg.get("confidence", 0.0) < estimator.min_confidence:
                logger.emit("debug", "audio.doa.srp_phat", "doa_low_confidence", {"confidence": msg.get("confidence", 0.0)})
            bus.publish("audio.doa_heatmap", msg)

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
) -> List[np.ndarray]:
    tables: List[np.ndarray] = []
    angles_rad = np.deg2rad(angles_deg)
    dir_vectors = np.stack([np.cos(angles_rad), np.sin(angles_rad)], axis=1)
    for (i, j) in pairs:
        diff = positions[i] - positions[j]
        delays = (dir_vectors @ diff) / SPEED_OF_SOUND_M_S
        phase = np.exp(1j * 2.0 * math.pi * delays[:, None] * freqs_hz[None, :])
        tables.append(phase.astype(np.complex64))
    return tables


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

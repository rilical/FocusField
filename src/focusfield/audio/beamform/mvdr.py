"""focusfield.audio.beamform.mvdr

CONTRACT: inline (source: src/focusfield/audio/beamform/mvdr.md)
ROLE: MVDR beamformer (adaptive) with stability fallbacks.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
  - Topic: fusion.target_lock  Type: TargetLock
  - Topic: audio.vad  Type: AudioVad
OUTPUTS:
  - Topic: audio.enhanced.beamformed  Type: EnhancedAudio

CONFIG KEYS:
  - audio.beamformer.method: mvdr
  - audio.beamformer.no_lock_behavior: omni|mute|last_lock
  - audio.beamformer.use_last_lock_ms: hold last target
  - audio.beamformer.steering_smoothing_alpha: target bearing smoothing
  - audio.beamformer.channel_weights.enabled: enable per-channel attenuation
  - audio.beamformer.channel_weights.spatial_exponent: exponent for cosine lobe
  - audio.beamformer.channel_weights.dead_rms_threshold: hard-mute dead channels
  - audio.beamformer.channel_weights.min_snr_db: low SNR clamp
  - audio.beamformer.channel_weights.max_snr_db: high SNR clamp
  - audio.beamformer.channel_weights.max_clip_fraction: hard-mute clipping channels
  - audio.beamformer.noise_reference.ref_threshold: reference mic selector
  - audio.beamformer.noise_reference.update_when_speaking: update noise from refs
  - audio.beamformer.mvdr.nfft: FFT size
  - audio.beamformer.mvdr.refresh_ms: recompute weights interval
  - audio.beamformer.mvdr.noise_ema_alpha: covariance EMA factor
  - audio.beamformer.mvdr.diagonal_loading: diagonal loading (lambda)
  - audio.beamformer.mvdr.steering_update_deg: steering change threshold
  - audio.beamformer.mvdr.max_condition_number: instability threshold

PERF / TIMING:
  - per block rFFT; weight recompute throttled

FAILURE MODES:
  - unstable weights -> fall back to delay-and-sum style omni -> log mvdr_unstable

LOG EVENTS:
  - module=audio.beamform.mvdr, event=mvdr_unstable, payload keys=reason

TESTS:
  - synthetic tests validate basic steering and fallback
"""

from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

from focusfield.audio.doa.geometry import load_mic_positions
from focusfield.audio.fft_backend import irfft, rfft, rfftfreq
from focusfield.audio.mic_health import channel_health_vectors
from focusfield.core.clock import now_ns


SPEED_OF_SOUND_M_S = 343.0


@dataclass
class _MvdrState:
    positions_xy: np.ndarray  # (C, 2)
    channel_order: np.ndarray  # (C,)
    sample_rate: int
    nfft: int
    freq_hz: np.ndarray  # (F,)
    # Noise covariance per frequency: (F, C, C)
    rnn: np.ndarray
    # Cached weights: (F, C)
    weights: Optional[np.ndarray] = None
    weights_theta_deg: Optional[float] = None
    weights_t_ns: int = 0
    weights_cond: Optional[float] = None
    seq_out: int = 0
    # Channel noise rms tracking
    noise_rms: Optional[np.ndarray] = None
    last_fallback: bool = False
    # Optional spectral postfilter state.
    pf_noise_psd: Optional[np.ndarray] = None
    pf_speech_psd: Optional[np.ndarray] = None
    freq_low_hz: float = 120.0
    freq_high_hz: float = 4800.0


def start_mvdr(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    beam_cfg = config.get("audio", {}).get("beamformer", {})
    method = str(beam_cfg.get("method", "delay_and_sum")).lower()
    if method != "mvdr":
        return None

    try:
        positions, channel_order = load_mic_positions(config)
    except Exception as exc:  # noqa: BLE001
        logger.emit("error", "audio.beamform.mvdr", "geometry_missing", {"error": str(exc)})
        return None

    audio_cfg = config.get("audio", {})
    sample_rate = int(audio_cfg.get("sample_rate_hz", 48000))
    channels = int(audio_cfg.get("channels", len(channel_order)))
    mvdr_cfg = beam_cfg.get("mvdr", {}) if isinstance(beam_cfg, dict) else {}
    if not isinstance(mvdr_cfg, dict):
        mvdr_cfg = {}
    weight_interp_alpha = float(mvdr_cfg.get("weight_interp_alpha", 0.35))
    weight_interp_alpha = float(min(1.0, max(1e-3, weight_interp_alpha)))
    speech_freeze_covariance = bool(mvdr_cfg.get("speech_freeze_covariance", True))
    freq_low_hz = float(mvdr_cfg.get("freq_low_hz", 120.0))
    freq_high_hz = float(mvdr_cfg.get("freq_high_hz", 4800.0))
    if freq_high_hz <= freq_low_hz:
        freq_high_hz = freq_low_hz + 1.0

    nfft = int(mvdr_cfg.get("nfft", int(audio_cfg.get("block_size", 1024))))
    nfft = max(256, int(2 ** round(math.log2(max(256, nfft)))))
    freq_hz = rfftfreq(nfft, d=1.0 / sample_rate).astype(np.float32)

    c = len(channel_order)
    if channels and c != channels:
        # Geometry loader already validates order vs channels, but be defensive.
        c = min(c, channels)
    f_bins = freq_hz.shape[0]
    rnn = np.zeros((f_bins, c, c), dtype=np.complex64)
    for f in range(f_bins):
        rnn[f] = np.eye(c, dtype=np.complex64) * 1e-3

    state = _MvdrState(
        positions_xy=np.asarray(positions, dtype=np.float32)[:c],
        channel_order=np.asarray(channel_order, dtype=np.int64)[:c],
        sample_rate=sample_rate,
        nfft=nfft,
        freq_hz=freq_hz,
        rnn=rnn,
        noise_rms=np.ones((c,), dtype=np.float32) * 1e-3,
        pf_noise_psd=np.ones((f_bins,), dtype=np.float32) * 1e-6,
        pf_speech_psd=np.ones((f_bins,), dtype=np.float32) * 1e-6,
        freq_low_hz=freq_low_hz,
        freq_high_hz=freq_high_hz,
    )

    no_lock_behavior = str(beam_cfg.get("no_lock_behavior", "omni")).lower()
    use_last_lock_ms = float(beam_cfg.get("use_last_lock_ms", 800.0))
    steering_alpha = float(beam_cfg.get("steering_smoothing_alpha", 0.85))
    refresh_ms = float(mvdr_cfg.get("refresh_ms", 250.0))
    noise_ema_alpha = float(mvdr_cfg.get("noise_ema_alpha", 0.95))
    diagonal_loading = float(mvdr_cfg.get("diagonal_loading", 1e-3))
    steering_update_deg = float(mvdr_cfg.get("steering_update_deg", 5.0))
    max_cond = float(mvdr_cfg.get("max_condition_number", 1e6))
    debug_hz = float(beam_cfg.get("debug_hz", 10.0))
    debug_period_ns = int(1e9 / max(0.1, debug_hz))
    pf_cfg = mvdr_cfg.get("postfilter", {}) if isinstance(mvdr_cfg, dict) else {}
    if not isinstance(pf_cfg, dict):
        pf_cfg = {}
    pf_enabled = bool(pf_cfg.get("enabled", True))
    pf_noise_ema_alpha = float(pf_cfg.get("noise_ema_alpha", 0.97))
    pf_speech_ema_alpha = float(pf_cfg.get("speech_ema_alpha", 0.90))
    pf_over_subtraction = float(pf_cfg.get("over_subtraction", 1.15))
    pf_min_gain = float(pf_cfg.get("min_gain", 0.08))

    weights_cfg = beam_cfg.get("channel_weights", {}) if isinstance(beam_cfg, dict) else {}
    if not isinstance(weights_cfg, dict):
        weights_cfg = {}
    weights_enabled = bool(weights_cfg.get("enabled", True))
    spatial_exponent = float(weights_cfg.get("spatial_exponent", 2.0))
    dead_rms_threshold = float(weights_cfg.get("dead_rms_threshold", 1e-5))
    min_snr_db = float(weights_cfg.get("min_snr_db", 3.0))
    max_snr_db = float(weights_cfg.get("max_snr_db", 18.0))
    max_clip_fraction = float(weights_cfg.get("max_clip_fraction", 0.01))

    fallback_cfg = beam_cfg.get("health_fallback", {}) if isinstance(beam_cfg, dict) else {}
    if not isinstance(fallback_cfg, dict):
        fallback_cfg = {}
    min_active_score = float(fallback_cfg.get("min_active_score", 0.35))
    mvdr_min_channels = int(fallback_cfg.get("mvdr_min_channels", 4))
    uncertain_trust_threshold = float(fallback_cfg.get("uncertain_trust_threshold", 0.45))

    noise_ref_cfg = beam_cfg.get("noise_reference", {}) if isinstance(beam_cfg, dict) else {}
    if not isinstance(noise_ref_cfg, dict):
        noise_ref_cfg = {}
    ref_threshold = float(noise_ref_cfg.get("ref_threshold", 0.2))
    update_when_speaking = bool(noise_ref_cfg.get("update_when_speaking", True))

    q_frames = bus.subscribe("audio.frames")
    q_lock = bus.subscribe("fusion.target_lock")
    q_vad = bus.subscribe("audio.vad")
    q_mic_health = bus.subscribe("audio.mic_health")
    last_lock: Optional[Dict[str, Any]] = None
    last_vad: Optional[Dict[str, Any]] = None
    last_mic_health: Optional[Dict[str, Any]] = None
    last_target: Optional[Tuple[float, int]] = None
    last_debug_ns = 0
    debug_seq = 0

    def _drain_latest(q: queue.Queue) -> Optional[Dict[str, Any]]:
        item = None
        try:
            while True:
                item = q.get_nowait()
        except queue.Empty:
            pass
        return item

    def _wait_and_drain_latest_frame(q_in: queue.Queue, timeout_s: float = 0.05) -> Optional[Dict[str, Any]]:
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
        nonlocal last_lock, last_vad, last_mic_health, last_target, last_debug_ns, debug_seq
        idle_cycles = 0
        processed_cycles = 0
        next_stats_emit = time.time() + 1.0
        while not stop_event.is_set():
            last_lock = _drain_latest(q_lock) or last_lock
            last_vad = _drain_latest(q_vad) or last_vad
            last_mic_health = _drain_latest(q_mic_health) or last_mic_health
            frame_msg = _wait_and_drain_latest_frame(q_frames, timeout_s=0.05)
            if frame_msg is None:
                idle_cycles += 1
            else:
                data = frame_msg.get("data")
                if data is None:
                    idle_cycles += 1
                else:
                    frame = np.asarray(data)
                    if frame.ndim == 1:
                        frame = frame[:, None]
                    if frame.shape[1] < state.channel_order.size:
                        idle_cycles += 1
                    else:
                        t_ns = int(frame_msg.get("t_ns", now_ns()))
                        x = frame[:, state.channel_order]
                        block_len = int(x.shape[0])
                        speech = bool(last_vad.get("speech")) if last_vad else False
                        health_scores, health_trust = channel_health_vectors(last_mic_health, x.shape[1])
                        active_idx = _active_channels(health_scores, health_trust, min_active_score)
                        if active_idx.size == 0:
                            active_idx = np.arange(x.shape[1], dtype=np.int64)

                        target_bearing = _select_target_bearing(last_lock, t_ns, last_target, use_last_lock_ms)
                        if target_bearing is None:
                            y = _no_lock_output(x, behavior=no_lock_behavior)
                            state.last_fallback = True
                            gains = np.ones((x.shape[1],), dtype=np.float32)
                            g_spatial = np.ones((x.shape[1],), dtype=np.float32)
                            beam_mode = "NO_LOCK"
                        else:
                            if last_target is None:
                                smoothed = target_bearing
                            else:
                                smoothed = _smooth_angle(last_target[0], target_bearing, steering_alpha)
                            last_target = (smoothed, t_ns)

                            gains, g_spatial = _channel_gains(
                                x,
                                state.positions_xy,
                                smoothed,
                                state.noise_rms,
                                health_scores,
                                health_trust,
                                weights_enabled,
                                spatial_exponent,
                                dead_rms_threshold,
                                min_snr_db,
                                max_snr_db,
                                max_clip_fraction,
                            )
                            mean_trust = float(np.mean(health_trust[active_idx])) if active_idx.size else 0.0
                            if mean_trust < uncertain_trust_threshold:
                                y = _subset_average(x, active_idx)
                                state.last_fallback = True
                                beam_mode = "HEALTH_AVERAGE"
                            elif active_idx.size == 1:
                                y = x[:, int(active_idx[0])].astype(np.float32)
                                state.last_fallback = True
                                beam_mode = "BEST_MIC"
                            elif active_idx.size < mvdr_min_channels:
                                y = _delay_and_sum_subset(x[:, active_idx], state.positions_xy[active_idx], smoothed, sample_rate)
                                state.last_fallback = True
                                beam_mode = "DELAY_SUM_SUBSET"
                            else:
                                _update_noise_rms(state.noise_rms, x, speech, noise_ema_alpha, update_when_speaking, g_spatial, ref_threshold)
                                x_fft = _frame_fft_from_msg(frame_msg, x, state.nfft, state.channel_order) * gains[None, :].astype(np.complex64)
                                if not (speech_freeze_covariance and speech):
                                    _update_noise_covariance(
                                        state,
                                        x_fft,
                                        speech,
                                        noise_ema_alpha,
                                        update_when_speaking,
                                        g_spatial,
                                        ref_threshold,
                                    )
                                y = _mvdr_apply(
                                    state,
                                    x_fft,
                                    smoothed,
                                    block_len,
                                    t_ns,
                                    refresh_ms,
                                    diagonal_loading,
                                    steering_update_deg,
                                    max_cond,
                                    weight_interp_alpha,
                                    logger,
                                )
                                beam_mode = "MVDR"

                        pf_stats = {"enabled": bool(pf_enabled), "gain_mean": 1.0}
                        if pf_enabled and y.size > 0:
                            y, pf_gain_mean = _mvdr_postfilter_block(
                                state=state,
                                y=y,
                                speech=bool(speech),
                                noise_ema_alpha=pf_noise_ema_alpha,
                                speech_ema_alpha=pf_speech_ema_alpha,
                                over_subtraction=pf_over_subtraction,
                                min_gain=pf_min_gain,
                            )
                            pf_stats["gain_mean"] = float(pf_gain_mean)

                        used_target = float(last_target[0]) if (target_bearing is not None and last_target is not None) else None
                        fallback_active = bool(target_bearing is None) or bool(state.last_fallback)
                        weights_age_ms = (t_ns - state.weights_t_ns) / 1_000_000.0 if state.weights_t_ns else None
                        if debug_period_ns > 0 and (t_ns - last_debug_ns) >= debug_period_ns:
                            debug_seq += 1
                            last_debug_ns = t_ns
                            ref_mask = (g_spatial < ref_threshold).astype(bool).tolist() if g_spatial is not None else []
                            bus.publish(
                                "audio.beamformer.debug",
                                {
                                    "t_ns": t_ns,
                                    "seq": debug_seq,
                                    "method": "mvdr",
                                    "target_bearing_deg": used_target,
                                    "beam_mode": beam_mode,
                                    "gains": gains.astype(np.float32).tolist() if gains is not None else [],
                                    "g_spatial": g_spatial.astype(np.float32).tolist() if g_spatial is not None else [],
                                    "active_channels": active_idx.astype(int).tolist(),
                                    "mic_health_score_mean": float(np.mean(health_scores)) if health_scores.size else 0.0,
                                    "mic_health_trust_mean": float(np.mean(health_trust)) if health_trust.size else 0.0,
                                    "ref_mask": ref_mask,
                                    "mvdr_condition_number": float(state.weights_cond) if state.weights_cond is not None else None,
                                    "weights_age_ms": float(weights_age_ms) if weights_age_ms is not None else None,
                                    "fallback_active": bool(fallback_active),
                                    "postfilter": pf_stats,
                                },
                            )

                        state.seq_out += 1
                        msg = {
                            "t_ns": t_ns,
                            "seq": int(state.seq_out),
                            "sample_rate_hz": int(frame_msg.get("sample_rate_hz", state.sample_rate)),
                            "frame_samples": int(y.shape[0]),
                            "channels": 1,
                            "data": y.astype(np.float32),
                            "stats": {
                                "rms": float(np.sqrt(np.mean(y**2))) if y.size else 0.0,
                            },
                        }
                        bus.publish("audio.enhanced.beamformed", msg)
                        processed_cycles += 1

            now_s = time.time()
            if now_s >= next_stats_emit:
                bus.publish(
                    "runtime.worker_loop",
                    {
                        "t_ns": now_ns(),
                        "module": "audio.beamform.mvdr",
                        "idle_cycles": int(idle_cycles),
                        "processed_cycles": int(processed_cycles),
                    },
                )
                next_stats_emit = now_s + 1.0

    thread = threading.Thread(target=_run, name="beamform-mvdr", daemon=True)
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


def _channel_gains(
    x: np.ndarray,
    positions_xy: np.ndarray,
    target_bearing_deg: float,
    noise_rms: Optional[np.ndarray],
    mic_health_scores: Optional[np.ndarray],
    mic_health_trust: Optional[np.ndarray],
    enabled: bool,
    spatial_exponent: float,
    dead_rms_threshold: float,
    min_snr_db: float,
    max_snr_db: float,
    max_clip_fraction: float,
) -> Tuple[np.ndarray, np.ndarray]:
    channels = x.shape[1]
    if not enabled:
        return np.ones((channels,), dtype=np.float32), np.ones((channels,), dtype=np.float32)

    phi = np.arctan2(positions_xy[:, 1], positions_xy[:, 0])  # radians
    theta = np.deg2rad(target_bearing_deg)
    delta = _wrap_rad(phi - theta)
    spatial = 0.5 * (1.0 + np.cos(delta))
    spatial = np.clip(spatial, 0.0, 1.0).astype(np.float32)
    spatial = np.power(spatial, float(max(0.1, spatial_exponent))).astype(np.float32)

    # Center mic (phi==0,0 position) gets neutral spatial weight.
    center_mask = (np.abs(positions_xy[:, 0]) < 1e-9) & (np.abs(positions_xy[:, 1]) < 1e-9)
    if np.any(center_mask):
        spatial[center_mask] = 1.0

    rms = np.sqrt(np.mean(x**2, axis=0)).astype(np.float32)
    clip_fraction = np.mean(np.abs(x) >= 0.999, axis=0).astype(np.float32)
    quality = np.ones((channels,), dtype=np.float32)

    # Dead / clipping hard mute.
    quality = np.where(rms < dead_rms_threshold, 0.0, quality)
    quality = np.where(clip_fraction >= max_clip_fraction, 0.0, quality)

    if noise_rms is not None:
        snr_db = 20.0 * np.log10(rms / (noise_rms + 1e-12) + 1e-12)
        snr_norm = (snr_db - min_snr_db) / max(1e-6, (max_snr_db - min_snr_db))
        snr_norm = np.clip(snr_norm, 0.0, 1.0).astype(np.float32)
        quality *= snr_norm
    if mic_health_scores is not None:
        quality *= np.clip(mic_health_scores, 0.0, 1.0).astype(np.float32)
    if mic_health_trust is not None:
        quality *= np.clip(0.35 + 0.65 * mic_health_trust, 0.0, 1.0).astype(np.float32)

    gains = spatial * quality
    return gains.astype(np.float32), spatial.astype(np.float32)


def _update_noise_rms(
    noise_rms: Optional[np.ndarray],
    x: np.ndarray,
    speech: bool,
    ema_alpha: float,
    update_when_speaking: bool,
    g_spatial: np.ndarray,
    ref_threshold: float,
) -> None:
    if noise_rms is None:
        return
    rms = np.sqrt(np.mean(x**2, axis=0)).astype(np.float32)
    if not speech:
        noise_rms[:] = ema_alpha * noise_rms + (1.0 - ema_alpha) * rms
        return
    if not update_when_speaking:
        return
    ref_mask = g_spatial < ref_threshold
    if not np.any(ref_mask):
        return
    noise_rms[ref_mask] = ema_alpha * noise_rms[ref_mask] + (1.0 - ema_alpha) * rms[ref_mask]


def _update_noise_covariance(
    state: _MvdrState,
    x_fft: np.ndarray,
    speech: bool,
    ema_alpha: float,
    update_when_speaking: bool,
    g_spatial: np.ndarray,
    ref_threshold: float,
) -> None:
    # x_fft: (F, C)
    if not speech:
        xf = x_fft.astype(np.complex64)
        r = xf[:, :, None] * np.conj(xf[:, None, :])
        state.rnn = ema_alpha * state.rnn + (1.0 - ema_alpha) * r
        return

    if not update_when_speaking:
        return

    ref_mask = g_spatial < ref_threshold
    if not np.any(ref_mask):
        return

    idx = np.where(ref_mask)[0]
    xf_ref = x_fft[:, idx].astype(np.complex64)
    r_ref = xf_ref[:, :, None] * np.conj(xf_ref[:, None, :])
    state.rnn[:, idx[:, None], idx[None, :]] = (
        ema_alpha * state.rnn[:, idx[:, None], idx[None, :]] + (1.0 - ema_alpha) * r_ref
    )


def _mvdr_apply(
    state: _MvdrState,
    x_fft: np.ndarray,
    target_bearing_deg: float,
    block_len: int,
    t_ns: int,
    refresh_ms: float,
    diagonal_loading: float,
    steering_update_deg: float,
    max_condition_number: float,
    weight_interp_alpha: float,
    logger: Any,
) -> np.ndarray:
    state.last_fallback = False
    recompute = state.weights is None
    if not recompute and refresh_ms > 0:
        age_ms = (t_ns - state.weights_t_ns) / 1_000_000.0
        if age_ms >= refresh_ms:
            recompute = True
    if not recompute and state.weights_theta_deg is not None:
        if abs(_wrap_deg(target_bearing_deg - state.weights_theta_deg)) >= steering_update_deg:
            recompute = True

    if recompute:
        try:
            new_weights, state.weights_cond = _compute_mvdr_weights_with_stats(
                state.positions_xy,
                state.freq_hz,
                state.rnn,
                target_bearing_deg,
                diagonal_loading,
                max_condition_number,
                state.freq_low_hz,
                state.freq_high_hz,
            )
            if state.weights is not None:
                alpha = float(min(1.0, max(1e-3, weight_interp_alpha)))
                state.weights = ((1.0 - alpha) * state.weights + alpha * new_weights).astype(np.complex64)
            else:
                state.weights = new_weights
            state.weights_theta_deg = float(target_bearing_deg)
            state.weights_t_ns = int(t_ns)
        except Exception as exc:  # noqa: BLE001
            logger.emit("warning", "audio.beamform.mvdr", "mvdr_unstable", {"reason": str(exc)})
            state.last_fallback = True
            y_omni = np.mean(irfft(x_fft, n=state.nfft, axis=0), axis=1).astype(np.float32)
            return y_omni[:block_len]

    w = state.weights
    if w is None:
        state.last_fallback = True
        y_omni = np.mean(irfft(x_fft, n=state.nfft, axis=0), axis=1).astype(np.float32)
        return y_omni[:block_len]

    y_fft = np.sum(np.conj(w) * x_fft.astype(np.complex64), axis=1)
    y = irfft(y_fft, n=state.nfft).astype(np.float32)
    return y[:block_len]


def _compute_mvdr_weights(
    positions_xy: np.ndarray,
    freq_hz: np.ndarray,
    rnn: np.ndarray,
    target_bearing_deg: float,
    diagonal_loading: float,
    max_condition_number: float,
    freq_low_hz: float = 120.0,
    freq_high_hz: float = 4800.0,
) -> np.ndarray:
    weights, _cond = _compute_mvdr_weights_with_stats(
        positions_xy=positions_xy,
        freq_hz=freq_hz,
        rnn=rnn,
        target_bearing_deg=target_bearing_deg,
        diagonal_loading=diagonal_loading,
        max_condition_number=max_condition_number,
        freq_low_hz=freq_low_hz,
        freq_high_hz=freq_high_hz,
    )
    return weights


def _compute_mvdr_weights_with_stats(
    positions_xy: np.ndarray,
    freq_hz: np.ndarray,
    rnn: np.ndarray,
    target_bearing_deg: float,
    diagonal_loading: float,
    max_condition_number: float,
    freq_low_hz: float = 120.0,
    freq_high_hz: float = 4800.0,
) -> Tuple[np.ndarray, float]:
    c = positions_xy.shape[0]
    theta = np.deg2rad(target_bearing_deg)
    direction = np.array([np.cos(theta), np.sin(theta)], dtype=np.float32)
    delays_s = (positions_xy @ direction) / SPEED_OF_SOUND_M_S

    w_out = np.ones((freq_hz.shape[0], c), dtype=np.complex64) / np.complex64(float(c))
    eye = np.eye(c, dtype=np.complex64)
    solve_mask = (freq_hz > max(80.0, 1.0)) & (freq_hz >= freq_low_hz) & (freq_hz <= freq_high_hz)
    active_idx = np.where(solve_mask)[0]
    if active_idx.size == 0:
        return w_out, 0.0

    freq_active = freq_hz[active_idx].astype(np.float32)
    d_active = np.exp(-1j * 2.0 * math.pi * freq_active[:, None] * delays_s[None, :]).astype(np.complex64)
    trace_mean = np.real(np.trace(rnn[active_idx], axis1=1, axis2=2)).astype(np.float32) / max(1, c)
    if diagonal_loading <= 0.0:
        adaptive_load = np.zeros((active_idx.size,), dtype=np.float32)
    else:
        edge_weight = np.ones((active_idx.size,), dtype=np.float32)
        edge_weight = np.where(freq_active < max(300.0, freq_low_hz + 120.0), edge_weight + 0.55, edge_weight)
        edge_weight = np.where(freq_active > min(freq_high_hz - 120.0, 3800.0), edge_weight + 0.35, edge_weight)
        adaptive_load = np.maximum(1e-9, diagonal_loading * edge_weight * np.maximum(trace_mean, 1e-6)).astype(np.float32)

    r_loaded = rnn[active_idx] + eye[None, :, :] * adaptive_load[:, None, None].astype(np.complex64)
    if not np.all(np.isfinite(r_loaded)):
        raise ValueError("covariance contains non-finite values")
    cond = np.asarray(np.linalg.cond(r_loaded), dtype=np.float64).reshape(-1)
    cond_max = float(np.max(cond)) if cond.size else 0.0
    if not np.all(np.isfinite(cond)) or cond_max > max_condition_number:
        raise ValueError(f"covariance ill-conditioned cond={cond_max:.2e}")

    numerator = np.linalg.solve(r_loaded, d_active[..., None]).squeeze(-1)
    denom = np.sum(np.conj(d_active) * numerator, axis=1).astype(np.complex64) + np.complex64(1e-12)
    w_out[active_idx] = (numerator / denom[:, None]).astype(np.complex64)
    return w_out, cond_max


def _mvdr_postfilter_block(
    state: _MvdrState,
    y: np.ndarray,
    speech: bool,
    noise_ema_alpha: float,
    speech_ema_alpha: float,
    over_subtraction: float,
    min_gain: float,
) -> Tuple[np.ndarray, float]:
    if y.size == 0:
        return y, 1.0
    if state.pf_noise_psd is None or state.pf_speech_psd is None:
        return y, 1.0

    y_fft = rfft(y, n=state.nfft).astype(np.complex64)
    y_psd = (np.abs(y_fft) ** 2).astype(np.float32)

    if speech:
        state.pf_speech_psd = speech_ema_alpha * state.pf_speech_psd + (1.0 - speech_ema_alpha) * y_psd
    else:
        state.pf_noise_psd = noise_ema_alpha * state.pf_noise_psd + (1.0 - noise_ema_alpha) * y_psd

    denom = np.maximum(state.pf_speech_psd, 1e-12)
    noise_ratio = np.clip(state.pf_noise_psd / denom, 0.0, 1.0)
    power_gain = np.clip(1.0 - float(over_subtraction) * noise_ratio, float(min_gain) ** 2, 1.0)
    amp_gain = np.sqrt(power_gain).astype(np.float32)
    y_fft_filtered = y_fft * amp_gain.astype(np.complex64)
    y_filtered = irfft(y_fft_filtered, n=state.nfft).astype(np.float32)[: y.shape[0]]
    return y_filtered, float(np.mean(amp_gain))


def _no_lock_output(x: np.ndarray, behavior: str) -> np.ndarray:
    behavior = str(behavior or "").lower()
    if behavior == "mute":
        return np.zeros((x.shape[0],), dtype=np.float32)
    return np.mean(x, axis=1).astype(np.float32)


def _smooth_angle(prev_deg: float, next_deg: float, alpha: float) -> float:
    a = np.deg2rad(prev_deg)
    b = np.deg2rad(next_deg)
    va = np.array([np.cos(a), np.sin(a)])
    vb = np.array([np.cos(b), np.sin(b)])
    v = alpha * vb + (1.0 - alpha) * va
    angle = float(np.rad2deg(np.arctan2(v[1], v[0])))
    return angle % 360.0


def _wrap_rad(rad: np.ndarray) -> np.ndarray:
    return (rad + math.pi) % (2.0 * math.pi) - math.pi


def _wrap_deg(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _frame_fft_from_msg(frame_msg: Dict[str, Any], x: np.ndarray, nfft: int, channel_order: np.ndarray) -> np.ndarray:
    data_fft = frame_msg.get("data_fft")
    fft_n = int(frame_msg.get("fft_n", 0) or 0)
    if data_fft is not None and fft_n == nfft:
        spectrum = np.asarray(data_fft)
        if spectrum.ndim == 2 and spectrum.shape[1] >= channel_order.size:
            return spectrum[:, channel_order].astype(np.complex64, copy=False)
    return rfft(x, n=nfft, axis=0).astype(np.complex64)



def _active_channels(scores: np.ndarray, trust: np.ndarray, min_active_score: float) -> np.ndarray:
    active = np.where((scores >= float(min_active_score)) & (trust >= 0.2))[0]
    return np.asarray(active, dtype=np.int64)


def _subset_average(x: np.ndarray, active_idx: np.ndarray) -> np.ndarray:
    if active_idx.size == 0:
        return np.mean(x, axis=1).astype(np.float32)
    return np.mean(x[:, active_idx], axis=1).astype(np.float32)


def _delay_and_sum_subset(x: np.ndarray, positions_xy: np.ndarray, bearing_deg: float, sample_rate: int) -> np.ndarray:
    if x.size == 0:
        return np.zeros((0,), dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    theta = np.deg2rad(bearing_deg)
    direction = np.array([np.cos(theta), np.sin(theta)], dtype=np.float32)
    delays_s = (positions_xy @ direction) / SPEED_OF_SOUND_M_S
    n = int(x.shape[0])
    x_fft = rfft(x, axis=0).astype(np.complex64)
    freqs = rfftfreq(n, d=1.0 / sample_rate).astype(np.float32)
    phase = np.exp(-1j * 2.0 * np.pi * freqs[:, None] * delays_s[None, :]).astype(np.complex64)
    y_fft = np.sum(x_fft * phase, axis=1) / float(max(1, x.shape[1]))
    return irfft(y_fft, n=n).astype(np.float32)[: x.shape[0]]


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

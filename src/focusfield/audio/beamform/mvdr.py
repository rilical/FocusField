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
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

from focusfield.audio.doa.geometry import load_mic_positions
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

    nfft = int(mvdr_cfg.get("nfft", int(audio_cfg.get("block_size", 1024))))
    nfft = max(256, int(2 ** round(math.log2(max(256, nfft)))))
    freq_hz = np.fft.rfftfreq(nfft, d=1.0 / sample_rate).astype(np.float32)

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

    weights_cfg = beam_cfg.get("channel_weights", {}) if isinstance(beam_cfg, dict) else {}
    if not isinstance(weights_cfg, dict):
        weights_cfg = {}
    weights_enabled = bool(weights_cfg.get("enabled", True))
    spatial_exponent = float(weights_cfg.get("spatial_exponent", 2.0))
    dead_rms_threshold = float(weights_cfg.get("dead_rms_threshold", 1e-5))
    min_snr_db = float(weights_cfg.get("min_snr_db", 3.0))
    max_snr_db = float(weights_cfg.get("max_snr_db", 18.0))
    max_clip_fraction = float(weights_cfg.get("max_clip_fraction", 0.01))

    noise_ref_cfg = beam_cfg.get("noise_reference", {}) if isinstance(beam_cfg, dict) else {}
    if not isinstance(noise_ref_cfg, dict):
        noise_ref_cfg = {}
    ref_threshold = float(noise_ref_cfg.get("ref_threshold", 0.2))
    update_when_speaking = bool(noise_ref_cfg.get("update_when_speaking", True))

    q_frames = bus.subscribe("audio.frames")
    q_lock = bus.subscribe("fusion.target_lock")
    q_vad = bus.subscribe("audio.vad")
    last_lock: Optional[Dict[str, Any]] = None
    last_vad: Optional[Dict[str, Any]] = None
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

    def _run() -> None:
        nonlocal last_lock, last_vad, last_target, last_debug_ns, debug_seq
        while not stop_event.is_set():
            last_lock = _drain_latest(q_lock) or last_lock
            last_vad = _drain_latest(q_vad) or last_vad
            try:
                frame_msg = q_frames.get(timeout=0.1)
            except queue.Empty:
                continue
            data = frame_msg.get("data")
            if data is None:
                continue
            frame = np.asarray(data)
            if frame.ndim == 1:
                frame = frame[:, None]
            if frame.shape[1] < state.channel_order.size:
                continue

            t_ns = int(frame_msg.get("t_ns", now_ns()))
            x = frame[:, state.channel_order]
            block_len = int(x.shape[0])
            speech = bool(last_vad.get("speech")) if last_vad else False

            target_bearing = _select_target_bearing(last_lock, t_ns, last_target, use_last_lock_ms)
            if target_bearing is None:
                y = _no_lock_output(x, behavior=no_lock_behavior)
                state.last_fallback = True
                gains = np.ones((x.shape[1],), dtype=np.float32)
                g_spatial = np.ones((x.shape[1],), dtype=np.float32)
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
                    weights_enabled,
                    spatial_exponent,
                    dead_rms_threshold,
                    min_snr_db,
                    max_snr_db,
                    max_clip_fraction,
                )
                xw = x * gains[None, :]

                # Update per-channel noise rms.
                _update_noise_rms(state.noise_rms, x, speech, noise_ema_alpha, update_when_speaking, g_spatial, ref_threshold)

                # Update noise covariance.
                x_fft = np.fft.rfft(xw, n=state.nfft, axis=0)
                _update_noise_covariance(state, x_fft, speech, noise_ema_alpha, update_when_speaking, g_spatial, ref_threshold)

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
                    logger,
                )

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
                        "gains": gains.astype(np.float32).tolist() if gains is not None else [],
                        "g_spatial": g_spatial.astype(np.float32).tolist() if g_spatial is not None else [],
                        "ref_mask": ref_mask,
                        "mvdr_condition_number": float(state.weights_cond) if state.weights_cond is not None else None,
                        "weights_age_ms": float(weights_age_ms) if weights_age_ms is not None else None,
                        "fallback_active": bool(fallback_active),
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
    if (not speech) or update_when_speaking:
        if speech and update_when_speaking:
            # Update only from reference mics (low spatial gain).
            ref_mask = g_spatial < ref_threshold
            if not np.any(ref_mask):
                return
            x_fft = x_fft[:, ref_mask]
            # Expand covariance into full matrix on those indices.
            # For simplicity on Pi4: we update full Rnn using only ref channels projected.
            # This preserves behavior but is less precise.
            # Build pseudo-full vector where non-ref channels are zeros.
            full = np.zeros((state.freq_hz.shape[0], state.channel_order.size), dtype=np.complex64)
            full[:, ref_mask] = x_fft.astype(np.complex64)
            x_fft_full = full
        else:
            x_fft_full = x_fft.astype(np.complex64)

        # R = E[x x^H]
        xf = x_fft_full.astype(np.complex64)
        r = xf[:, :, None] * np.conj(xf[:, None, :])
        state.rnn = ema_alpha * state.rnn + (1.0 - ema_alpha) * r


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
            state.weights, state.weights_cond = _compute_mvdr_weights_with_stats(
                state.positions_xy,
                state.freq_hz,
                state.rnn,
                target_bearing_deg,
                diagonal_loading,
                max_condition_number,
            )
            state.weights_theta_deg = float(target_bearing_deg)
            state.weights_t_ns = int(t_ns)
        except Exception as exc:  # noqa: BLE001
            logger.emit("warning", "audio.beamform.mvdr", "mvdr_unstable", {"reason": str(exc)})
            state.last_fallback = True
            y_omni = np.mean(np.fft.irfft(x_fft, n=state.nfft, axis=0), axis=1).astype(np.float32)
            return y_omni[:block_len]

    w = state.weights
    if w is None:
        state.last_fallback = True
        y_omni = np.mean(np.fft.irfft(x_fft, n=state.nfft, axis=0), axis=1).astype(np.float32)
        return y_omni[:block_len]

    y_fft = np.sum(np.conj(w) * x_fft.astype(np.complex64), axis=1)
    y = np.fft.irfft(y_fft, n=state.nfft).astype(np.float32)
    return y[:block_len]


def _compute_mvdr_weights(
    positions_xy: np.ndarray,
    freq_hz: np.ndarray,
    rnn: np.ndarray,
    target_bearing_deg: float,
    diagonal_loading: float,
    max_condition_number: float,
) -> np.ndarray:
    weights, _cond = _compute_mvdr_weights_with_stats(
        positions_xy=positions_xy,
        freq_hz=freq_hz,
        rnn=rnn,
        target_bearing_deg=target_bearing_deg,
        diagonal_loading=diagonal_loading,
        max_condition_number=max_condition_number,
    )
    return weights


def _compute_mvdr_weights_with_stats(
    positions_xy: np.ndarray,
    freq_hz: np.ndarray,
    rnn: np.ndarray,
    target_bearing_deg: float,
    diagonal_loading: float,
    max_condition_number: float,
) -> Tuple[np.ndarray, float]:
    c = positions_xy.shape[0]
    theta = np.deg2rad(target_bearing_deg)
    direction = np.array([np.cos(theta), np.sin(theta)], dtype=np.float32)
    delays_s = (positions_xy @ direction) / SPEED_OF_SOUND_M_S

    w_out = np.zeros((freq_hz.shape[0], c), dtype=np.complex64)
    eye = np.eye(c, dtype=np.complex64)
    cond_max = 0.0
    for f, freq in enumerate(freq_hz):
        if freq <= 1.0:
            # DC/near-DC: omni
            w_out[f, :] = (1.0 / float(c))
            continue
        d = np.exp(-1j * 2.0 * math.pi * float(freq) * delays_s).astype(np.complex64)  # (C,)
        r_loaded = rnn[f] + eye * np.complex64(diagonal_loading)
        if not np.all(np.isfinite(r_loaded)):
            raise ValueError("covariance contains non-finite values")
        cond = float(np.linalg.cond(r_loaded))
        if cond > cond_max:
            cond_max = cond
        if not np.isfinite(cond) or cond > max_condition_number:
            raise ValueError(f"covariance ill-conditioned cond={cond:.2e}")
        r_inv = np.linalg.inv(r_loaded)
        denom = np.conj(d).T @ r_inv @ d
        denom = np.complex64(denom) + np.complex64(1e-12)
        w = (r_inv @ d) / denom
        w_out[f, :] = w
    return w_out, float(cond_max)


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

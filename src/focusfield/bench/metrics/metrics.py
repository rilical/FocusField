"""Metric definitions and computation for FocusBench."""

from __future__ import annotations

import json
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np


EPS = 1e-12


@dataclass(frozen=True)
class SceneMetric:
    scene_id: str
    target_angle_deg: Optional[float]
    interferer_angle_deg: Optional[float]
    si_sdr_baseline_db: Optional[float]
    si_sdr_candidate_db: Optional[float]
    si_sdr_delta_db: Optional[float]
    stoi_baseline: Optional[float]
    stoi_candidate: Optional[float]
    stoi_delta: Optional[float]
    sir_baseline_db: Optional[float]
    sir_candidate_db: Optional[float]
    sir_delta_db: Optional[float]
    wer_baseline: Optional[float]
    wer_candidate: Optional[float]
    wer_relative_improvement: Optional[float]


def load_wav(path: str | Path) -> Tuple[int, np.ndarray]:
    """Load WAV as float32 in range [-1, 1], shape (N, C)."""
    wav_path = Path(path)
    with wave.open(str(wav_path), "rb") as handle:
        sr = int(handle.getframerate())
        channels = int(handle.getnchannels())
        sample_width = int(handle.getsampwidth())
        frames = int(handle.getnframes())
        raw = handle.readframes(frames)

    if sample_width == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sample_width}")

    if channels <= 0:
        raise ValueError("Invalid WAV channel count")
    arr = arr.reshape(-1, channels)
    return sr, arr


def as_mono(x: np.ndarray) -> np.ndarray:
    if x.ndim == 1:
        return x.astype(np.float32, copy=False)
    if x.ndim != 2:
        raise ValueError(f"Expected mono/multi-channel audio, got shape={x.shape}")
    return np.mean(x.astype(np.float32), axis=1)


def align_pair(a: np.ndarray, b: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    n = min(int(a.shape[0]), int(b.shape[0]))
    if n <= 0:
        return np.zeros((0,), dtype=np.float32), np.zeros((0,), dtype=np.float32)
    return a[:n].astype(np.float32), b[:n].astype(np.float32)


def si_sdr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    """Scale-invariant SDR in dB."""
    s, e = align_pair(reference.reshape(-1), estimate.reshape(-1))
    if s.size == 0:
        return float("-inf")
    s = s - np.mean(s)
    e = e - np.mean(e)
    target_scale = float(np.dot(e, s) / (np.dot(s, s) + EPS))
    target = target_scale * s
    noise = e - target
    ratio = float((np.dot(target, target) + EPS) / (np.dot(noise, noise) + EPS))
    return 10.0 * math.log10(max(ratio, EPS))


def stoi_proxy(reference: np.ndarray, estimate: np.ndarray, sample_rate_hz: int) -> float:
    """Dependency-light intelligibility proxy in [0,1].

    This is not a full STOI implementation. It approximates intelligibility by
    averaging short-window normalized correlation in speech bands.
    """
    s, e = align_pair(reference.reshape(-1), estimate.reshape(-1))
    if s.size == 0:
        return 0.0

    # Band-pass-like emphasis with simple first-order difference.
    s_hp = np.concatenate([[s[0]], np.diff(s)])
    e_hp = np.concatenate([[e[0]], np.diff(e)])

    win_ms = 32
    hop_ms = 16
    win = max(64, int(sample_rate_hz * win_ms / 1000.0))
    hop = max(32, int(sample_rate_hz * hop_ms / 1000.0))
    if s_hp.size < win:
        return float(max(0.0, min(1.0, np.corrcoef(s_hp, e_hp)[0, 1] * 0.5 + 0.5)))

    corr_vals: List[float] = []
    for start in range(0, s_hp.size - win + 1, hop):
        sw = s_hp[start : start + win]
        ew = e_hp[start : start + win]
        sw = sw - np.mean(sw)
        ew = ew - np.mean(ew)
        denom = float(np.linalg.norm(sw) * np.linalg.norm(ew))
        if denom <= EPS:
            continue
        corr = float(np.dot(sw, ew) / denom)
        corr_vals.append(max(-1.0, min(1.0, corr)))
    if not corr_vals:
        return 0.0
    score = float(np.mean(corr_vals))
    return float(max(0.0, min(1.0, 0.5 * (score + 1.0))))


def sir_db(reference: np.ndarray, interference: np.ndarray, estimate: np.ndarray) -> float:
    """Compute SIR using projection of estimate onto target and interferer."""
    s, e = align_pair(reference.reshape(-1), estimate.reshape(-1))
    n, _ = align_pair(interference.reshape(-1), e)
    s, n = align_pair(s, n)
    if s.size == 0:
        return float("-inf")
    s = s - np.mean(s)
    n = n - np.mean(n)
    e = e[: s.size] - np.mean(e[: s.size])

    s_proj = (np.dot(e, s) / (np.dot(s, s) + EPS)) * s
    n_proj = (np.dot(e, n) / (np.dot(n, n) + EPS)) * n
    ratio = float((np.dot(s_proj, s_proj) + EPS) / (np.dot(n_proj, n_proj) + EPS))
    return 10.0 * math.log10(max(ratio, EPS))


def wer(reference_text: str, hypothesis_text: str) -> float:
    ref_tokens = _tokenize(reference_text)
    hyp_tokens = _tokenize(hypothesis_text)
    if not ref_tokens:
        return 0.0 if not hyp_tokens else 1.0
    dist = _levenshtein(ref_tokens, hyp_tokens)
    return float(dist / max(1, len(ref_tokens)))


def compute_latency_stats(perf_jsonl_path: str | Path) -> Dict[str, Optional[float]]:
    values: List[float] = []
    for row in _read_jsonl(perf_jsonl_path):
        enhanced = row.get("enhanced_final")
        if not isinstance(enhanced, dict):
            continue
        latency = enhanced.get("pipeline_queue_age_ms", enhanced.get("last_latency_ms"))
        if latency is None:
            continue
        try:
            values.append(float(latency))
        except Exception:
            continue
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "mean_ms": None}
    arr = np.asarray(values, dtype=np.float64)
    return {
        "count": int(arr.size),
        "p50_ms": float(np.percentile(arr, 50)),
        "p95_ms": float(np.percentile(arr, 95)),
        "p99_ms": float(np.percentile(arr, 99)),
        "mean_ms": float(np.mean(arr)),
    }


def compute_drop_stats(events_jsonl_path: str | Path, perf_jsonl_path: str | Path) -> Dict[str, float]:
    queue_full_total = 0
    queue_full_audio = 0
    underrun_total = 0
    for row in _read_jsonl(events_jsonl_path):
        ctx = row.get("context")
        if not isinstance(ctx, dict):
            continue
        module = str(ctx.get("module", ""))
        event = str(ctx.get("event", ""))
        details = ctx.get("details")
        if not isinstance(details, dict):
            details = {}
        if module == "core.bus" and event == "queue_full":
            queue_full_total += 1
            topic = str(details.get("topic", ""))
            if topic.startswith("audio."):
                queue_full_audio += 1
        if module == "audio.capture" and event == "underrun":
            underrun_total += 1

    frames = 0
    for row in _read_jsonl(perf_jsonl_path):
        enhanced = row.get("audio_frames")
        if isinstance(enhanced, dict):
            try:
                frames = max(frames, int(enhanced.get("count", 0)))
            except Exception:
                continue
    frame_denom = max(1, frames)
    return {
        "queue_full_total": float(queue_full_total),
        "queue_full_audio": float(queue_full_audio),
        "capture_underrun_total": float(underrun_total),
        "capture_underrun_rate": float(underrun_total / frame_denom),
    }


def compute_runtime_summary(perf_jsonl_path: str | Path) -> Dict[str, Optional[float]]:
    queue_pressure_samples: List[float] = []
    queue_pressure_elapsed_s = 0.0
    queue_pressure_events = 0.0
    output_underrun_events = 0.0
    output_underrun_elapsed_s = 0.0
    output_underrun_total = 0.0
    output_underrun_rate_samples: List[float] = []
    output_overrun_total = 0.0
    output_error_total = 0.0
    output_occupancy_ratio_samples: List[float] = []
    prev_t_ns: Optional[int] = None
    prev_underrun_total: Optional[float] = None

    for row in _read_jsonl(perf_jsonl_path):
        t_ns = _as_optional_int(row.get("t_ns"))
        queue_pressure = row.get("queue_pressure")
        if isinstance(queue_pressure, dict):
            pressure = _as_optional_float(
                queue_pressure.get("drop_total_window", queue_pressure.get("capture_overflow_window"))
            )
            if pressure is not None:
                queue_pressure_samples.append(pressure)
            if t_ns is not None and prev_t_ns is not None and t_ns > prev_t_ns:
                queue_pressure_elapsed_s += float(t_ns - prev_t_ns) / 1_000_000_000.0
                queue_pressure_events += pressure or 0.0

        output_stats = _extract_output_stats(row)
        if not isinstance(output_stats, dict):
            prev_t_ns = t_ns if t_ns is not None else prev_t_ns
            continue

        underrun_total = _as_optional_float(output_stats.get("underrun_total"))
        underrun_window = _as_optional_float(output_stats.get("underrun_window"))
        overrun_total = _as_optional_float(output_stats.get("overrun_total"))
        error_total = _as_optional_float(output_stats.get("device_error_total"))
        occupancy_frames = _as_optional_float(output_stats.get("occupancy_frames"))
        buffer_capacity_frames = _as_optional_float(output_stats.get("buffer_capacity_frames"))
        if underrun_total is not None:
            output_underrun_total = max(output_underrun_total, underrun_total)
        if overrun_total is not None:
            output_overrun_total = max(output_overrun_total, overrun_total)
        if error_total is not None:
            output_error_total = max(output_error_total, error_total)
        if occupancy_frames is not None and buffer_capacity_frames is not None and buffer_capacity_frames > 0.0:
            output_occupancy_ratio_samples.append(max(0.0, occupancy_frames / buffer_capacity_frames))

        if t_ns is not None and prev_t_ns is not None and t_ns > prev_t_ns:
            elapsed_s = float(t_ns - prev_t_ns) / 1_000_000_000.0
            if underrun_window is not None:
                output_underrun_events += max(0.0, underrun_window)
                output_underrun_elapsed_s += elapsed_s
                if elapsed_s > 0.0:
                    output_underrun_rate_samples.append(max(0.0, underrun_window) / elapsed_s)
            elif underrun_total is not None and prev_underrun_total is not None:
                delta = max(0.0, underrun_total - prev_underrun_total)
                output_underrun_events += delta
                output_underrun_elapsed_s += elapsed_s
                if elapsed_s > 0.0:
                    output_underrun_rate_samples.append(delta / elapsed_s)
        if underrun_total is not None:
            prev_underrun_total = underrun_total
        prev_t_ns = t_ns if t_ns is not None else prev_t_ns

    queue_pressure_peak = _as_optional_float(max(queue_pressure_samples)) if queue_pressure_samples else None
    queue_pressure_rate = (
        float(queue_pressure_events / queue_pressure_elapsed_s)
        if queue_pressure_elapsed_s > 0.0
        else None
    )
    if output_underrun_rate_samples:
        output_underrun_rate = float(np.mean(np.asarray(output_underrun_rate_samples, dtype=np.float64)))
    elif output_underrun_elapsed_s > 0.0:
        output_underrun_rate = float(output_underrun_events / output_underrun_elapsed_s)
    else:
        output_underrun_rate = None

    return {
        "queue_pressure_peak": queue_pressure_peak,
        "queue_pressure_rate": queue_pressure_rate,
        "output_underrun_total": float(output_underrun_total),
        "output_underrun_rate": output_underrun_rate,
        "output_overrun_total": float(output_overrun_total),
        "output_error_total": float(output_error_total),
        "output_occupancy_ratio_peak": max(output_occupancy_ratio_samples) if output_occupancy_ratio_samples else None,
    }


def compute_lock_jitter(lock_jsonl_path: str | Path) -> Dict[str, Optional[float]]:
    bearings: List[float] = []
    for row in _read_jsonl(lock_jsonl_path):
        bearing = row.get("target_bearing_deg")
        state = str(row.get("state", ""))
        if bearing is None:
            continue
        if state not in {"LOCKED", "HANDOFF", "HOLD"}:
            continue
        try:
            bearings.append(float(bearing) % 360.0)
        except Exception:
            continue
    if len(bearings) < 2:
        return {"samples": float(len(bearings)), "mae_step_deg": None, "std_step_deg": None, "rms_step_deg": None}

    deltas: List[float] = []
    prev = bearings[0]
    for current in bearings[1:]:
        delta = _wrap_deg(current - prev)
        deltas.append(abs(delta))
        prev = current
    arr = np.asarray(deltas, dtype=np.float64)
    return {
        "samples": float(len(bearings)),
        "mae_step_deg": float(np.mean(arr)),
        "std_step_deg": float(np.std(arr)),
        "rms_step_deg": float(np.sqrt(np.mean(np.square(arr)))),
    }


def compute_conversation_metrics(
    lock_jsonl_path: str | Path,
    faces_jsonl_path: str | Path,
    events_jsonl_path: str | Path,
) -> Dict[str, Optional[float]]:
    """Compute interruption/handoff and reacquire quality metrics for conversational scenes."""

    handoff_start_ns: Optional[int] = None
    handoff_latencies_ms: List[float] = []
    switch_count = 0
    false_switch_count = 0
    last_target_id: Optional[str] = None
    last_switch_ns: Optional[int] = None
    false_window_ns = int(700 * 1_000_000)  # rapid switch-back window

    for row in _read_jsonl(lock_jsonl_path):
        t_ns = int(row.get("t_ns", 0) or 0)
        reason = str(row.get("reason", "") or "")
        state = str(row.get("state", "") or "")
        target_id_raw = row.get("target_id")
        target_id = str(target_id_raw) if target_id_raw is not None else None

        if reason == "handoff_start":
            handoff_start_ns = t_ns
        elif reason == "handoff_commit" and handoff_start_ns and t_ns >= handoff_start_ns:
            handoff_latencies_ms.append((t_ns - handoff_start_ns) / 1_000_000.0)
            handoff_start_ns = None

        if state in {"LOCKED", "HANDOFF"} and target_id:
            if last_target_id and target_id != last_target_id:
                switch_count += 1
                if last_switch_ns and (t_ns - last_switch_ns) <= false_window_ns:
                    false_switch_count += 1
                last_switch_ns = t_ns
            last_target_id = target_id

    face_reacquire_ms: List[float] = []
    faces_present = False
    faces_absent_since_ns: Optional[int] = None
    for row in _read_jsonl(faces_jsonl_path):
        t_ns = int(row.get("t_ns", 0) or 0)
        faces = row.get("faces")
        count = len(faces) if isinstance(faces, list) else 0
        present = count > 0
        if present and not faces_present and faces_absent_since_ns is not None and t_ns >= faces_absent_since_ns:
            face_reacquire_ms.append((t_ns - faces_absent_since_ns) / 1_000_000.0)
        if (not present) and faces_present:
            faces_absent_since_ns = t_ns
        faces_present = present

    speech_no_lock = 0
    speech_samples = 0
    for row in _read_jsonl(events_jsonl_path):
        ctx = row.get("context")
        if not isinstance(ctx, dict):
            continue
        if str(ctx.get("module", "") or "") != "fusion.av_association":
            continue
        if str(ctx.get("event", "") or "") != "no_candidates":
            continue
        details = ctx.get("details")
        if not isinstance(details, dict):
            continue
        if bool(details.get("vad_speech", False)):
            speech_no_lock += 1
            speech_samples += 1
        elif "vad_speech" in details:
            speech_samples += 1

    no_lock_during_speech_ratio = (
        float(speech_no_lock) / float(max(1, speech_samples))
        if speech_samples > 0
        else None
    )
    false_handoff_rate = (
        float(false_switch_count) / float(max(1, switch_count))
        if switch_count > 0
        else 0.0
    )

    return {
        "handoff_latency_p50_ms": _percentile_list(handoff_latencies_ms, 50.0),
        "handoff_latency_p95_ms": _percentile_list(handoff_latencies_ms, 95.0),
        "no_lock_during_speech_ratio": no_lock_during_speech_ratio,
        "face_reacquire_latency_p50_ms": _percentile_list(face_reacquire_ms, 50.0),
        "face_reacquire_latency_p95_ms": _percentile_list(face_reacquire_ms, 95.0),
        "false_handoff_rate": false_handoff_rate,
        "handoff_count": float(len(handoff_latencies_ms)),
        "switch_count": float(switch_count),
    }


def compute_label_scene_metrics(
    scene: Dict[str, Any],
    lock_jsonl_path: str | Path,
    faces_jsonl_path: str | Path,
    conversation_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[float]]:
    """Compute label-aware metrics from optional recorded scene annotations."""

    labels = _scene_labels(scene)
    lock_rows = _sorted_rows(_read_jsonl(lock_jsonl_path), "t_ns")

    speaker_segments = _normalize_segments(_scene_segments(labels, "speaker_segments", "speaker_timeline", "speaker_labels"))
    bearing_segments = _normalize_segments(_scene_segments(labels, "bearing_segments", "bearing_timeline", "target_bearing_segments"))
    track_segments = _normalize_segments(_scene_segments(labels, "track_segments", "identity_segments", "identity_timeline"))
    face_segments = _normalize_segments(_scene_segments(labels, "face_segments", "face_presence_segments", "face_timeline"))

    selection = _compute_selection_accuracy(lock_rows, speaker_segments)
    steering = _compute_steering_error(lock_rows, bearing_segments)
    churn = _compute_id_churn(track_segments if track_segments else speaker_segments)
    face_reacquire = _compute_face_reacquire(face_segments)
    if face_reacquire["face_reacquire_latency_p50_ms"] is None or face_reacquire["face_reacquire_latency_p95_ms"] is None:
        face_reacquire = _merge_face_reacquire(face_reacquire, conversation_summary)

    return {
        **selection,
        **steering,
        **churn,
        **face_reacquire,
    }


def summarize_label_scene_metrics(scene_label_metrics: Iterable[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    metrics = [item for item in scene_label_metrics if isinstance(item, dict)]
    if not metrics:
        return {
            "scene_count": 0.0,
            "label_supported_scene_count": 0.0,
            "speaker_selection_accuracy": None,
            "steering_mae_deg": None,
            "steering_rmse_deg": None,
            "id_churn_rate": None,
            "id_switch_count": None,
            "face_reacquire_latency_p50_ms": None,
            "face_reacquire_latency_p95_ms": None,
        }

    label_supported_scene_count = 0.0
    speaker_selected_duration = 0.0
    speaker_correct_duration = 0.0
    steering_weight = 0.0
    steering_abs_weighted = 0.0
    steering_sq_weighted = 0.0
    id_switch_count = 0.0
    id_track_duration_s = 0.0
    face_reacquire_p50: List[float] = []
    face_reacquire_p95: List[float] = []

    for item in metrics:
        if any(_as_optional_float(item.get(key)) is not None for key in ("speaker_selection_accuracy", "steering_mae_deg", "id_churn_rate")):
            label_supported_scene_count += 1.0
        selected_duration = _as_optional_float(item.get("speaker_selection_duration_s")) or 0.0
        correct_duration = _as_optional_float(item.get("speaker_selection_correct_duration_s")) or 0.0
        speaker_selected_duration += selected_duration
        speaker_correct_duration += correct_duration

        steering_duration = _as_optional_float(item.get("steering_duration_s")) or 0.0
        steering_weight += steering_duration
        steering_mae = _as_optional_float(item.get("steering_mae_deg"))
        steering_rmse = _as_optional_float(item.get("steering_rmse_deg"))
        if steering_mae is not None:
            steering_abs_weighted += steering_mae * steering_duration
        if steering_rmse is not None:
            steering_sq_weighted += (steering_rmse * steering_rmse) * steering_duration

        id_switch_count += _as_optional_float(item.get("id_switch_count")) or 0.0
        id_track_duration_s += _as_optional_float(item.get("id_track_duration_s")) or 0.0

        face_p50 = _as_optional_float(item.get("face_reacquire_latency_p50_ms"))
        face_p95 = _as_optional_float(item.get("face_reacquire_latency_p95_ms"))
        if face_p50 is not None:
            face_reacquire_p50.append(face_p50)
        if face_p95 is not None:
            face_reacquire_p95.append(face_p95)

    duration_min = id_track_duration_s / 60.0 if id_track_duration_s > 0.0 else 0.0
    return {
        "scene_count": float(len(metrics)),
        "label_supported_scene_count": float(label_supported_scene_count),
        "speaker_selection_accuracy": float(speaker_correct_duration / speaker_selected_duration) if speaker_selected_duration > 0.0 else None,
        "steering_mae_deg": float(steering_abs_weighted / steering_weight) if steering_weight > 0.0 else None,
        "steering_rmse_deg": float(np.sqrt(steering_sq_weighted / steering_weight)) if steering_weight > 0.0 else None,
        "id_churn_rate": float(id_switch_count / duration_min) if duration_min > 0.0 else None,
        "id_switch_count": float(id_switch_count),
        "face_reacquire_latency_p50_ms": _median(face_reacquire_p50),
        "face_reacquire_latency_p95_ms": _median(face_reacquire_p95),
    }


def compute_scene_metric(
    scene: Dict[str, Any],
    baseline_audio_path: str | Path,
    candidate_audio_path: str | Path,
) -> SceneMetric:
    scene_id = str(scene.get("scene_id", "scene"))
    target_angle = _as_optional_float(scene.get("target_angle_deg"))
    interferer_angle = _as_optional_float(scene.get("interferer_angle_deg"))

    baseline_sr, baseline_audio = load_wav(baseline_audio_path)
    candidate_sr, candidate_audio = load_wav(candidate_audio_path)
    if baseline_sr != candidate_sr:
        raise ValueError(f"Sample rate mismatch: baseline={baseline_sr} candidate={candidate_sr}")
    clip_bounds = _scene_clip_bounds(scene)
    baseline_audio = _clip_audio_window(baseline_audio, baseline_sr, clip_bounds)
    candidate_audio = _clip_audio_window(candidate_audio, candidate_sr, clip_bounds)
    baseline_mono = as_mono(baseline_audio)
    candidate_mono = as_mono(candidate_audio)

    ref_target_path = scene.get("reference_audio_path") or scene.get("target_reference_wav")
    ref_noise_path = scene.get("noise_reference_audio_path") or scene.get("interferer_reference_wav")

    si_sdr_base = None
    si_sdr_cand = None
    stoi_base = None
    stoi_cand = None
    sir_base = None
    sir_cand = None
    if ref_target_path:
        ref_sr, ref_target = load_wav(ref_target_path)
        if ref_sr != baseline_sr:
            raise ValueError(f"Reference sample rate mismatch in scene={scene_id}")
        ref_target = _clip_audio_window(ref_target, ref_sr, clip_bounds)
        ref_target_mono = as_mono(ref_target)
        si_sdr_base = si_sdr_db(ref_target_mono, baseline_mono)
        si_sdr_cand = si_sdr_db(ref_target_mono, candidate_mono)
        stoi_base = stoi_proxy(ref_target_mono, baseline_mono, baseline_sr)
        stoi_cand = stoi_proxy(ref_target_mono, candidate_mono, baseline_sr)

        if ref_noise_path:
            noise_sr, ref_noise = load_wav(ref_noise_path)
            if noise_sr != baseline_sr:
                raise ValueError(f"Interference sample rate mismatch in scene={scene_id}")
            ref_noise = _clip_audio_window(ref_noise, noise_sr, clip_bounds)
            ref_noise_mono = as_mono(ref_noise)
            sir_base = sir_db(ref_target_mono, ref_noise_mono, baseline_mono)
            sir_cand = sir_db(ref_target_mono, ref_noise_mono, candidate_mono)

    reference_text = str(scene.get("reference_text", "") or "")
    baseline_text = str(scene.get("baseline_text", "") or "")
    candidate_text = str(scene.get("candidate_text", "") or "")
    wer_base = None
    wer_cand = None
    wer_rel = None
    if reference_text and baseline_text and candidate_text:
        wer_base = wer(reference_text, baseline_text)
        wer_cand = wer(reference_text, candidate_text)
        if wer_base > EPS:
            wer_rel = float((wer_base - wer_cand) / wer_base)
        else:
            wer_rel = 0.0

    return SceneMetric(
        scene_id=scene_id,
        target_angle_deg=target_angle,
        interferer_angle_deg=interferer_angle,
        si_sdr_baseline_db=si_sdr_base,
        si_sdr_candidate_db=si_sdr_cand,
        si_sdr_delta_db=_delta(si_sdr_base, si_sdr_cand),
        stoi_baseline=stoi_base,
        stoi_candidate=stoi_cand,
        stoi_delta=_delta(stoi_base, stoi_cand),
        sir_baseline_db=sir_base,
        sir_candidate_db=sir_cand,
        sir_delta_db=_delta(sir_base, sir_cand),
        wer_baseline=wer_base,
        wer_candidate=wer_cand,
        wer_relative_improvement=wer_rel,
    )


def summarize_scene_metrics(scene_metrics: Iterable[SceneMetric]) -> Dict[str, Optional[float]]:
    metrics = list(scene_metrics)
    return {
        "scene_count": float(len(metrics)),
        "median_si_sdr_delta_db": _median([m.si_sdr_delta_db for m in metrics]),
        "median_stoi_delta": _median([m.stoi_delta for m in metrics]),
        "median_sir_delta_db": _median([m.sir_delta_db for m in metrics]),
        "median_wer_relative_improvement": _median([m.wer_relative_improvement for m in metrics]),
    }


def scene_metric_to_dict(scene_metric: SceneMetric) -> Dict[str, Any]:
    return {
        "scene_id": scene_metric.scene_id,
        "target_angle_deg": scene_metric.target_angle_deg,
        "interferer_angle_deg": scene_metric.interferer_angle_deg,
        "si_sdr_baseline_db": scene_metric.si_sdr_baseline_db,
        "si_sdr_candidate_db": scene_metric.si_sdr_candidate_db,
        "si_sdr_delta_db": scene_metric.si_sdr_delta_db,
        "stoi_baseline": scene_metric.stoi_baseline,
        "stoi_candidate": scene_metric.stoi_candidate,
        "stoi_delta": scene_metric.stoi_delta,
        "sir_baseline_db": scene_metric.sir_baseline_db,
        "sir_candidate_db": scene_metric.sir_candidate_db,
        "sir_delta_db": scene_metric.sir_delta_db,
        "wer_baseline": scene_metric.wer_baseline,
        "wer_candidate": scene_metric.wer_candidate,
        "wer_relative_improvement": scene_metric.wer_relative_improvement,
    }


def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return float(b - a)


def _scene_clip_bounds(scene: Dict[str, Any]) -> Optional[Tuple[float, Optional[float]]]:
    clip_cfg = scene.get("clip")
    start = _as_optional_float(scene.get("start_s", scene.get("clip_start_s")))
    end = _as_optional_float(scene.get("end_s", scene.get("clip_end_s")))
    if isinstance(clip_cfg, dict):
        if start is None:
            start = _as_optional_float(clip_cfg.get("start_s", clip_cfg.get("start")))
        if end is None:
            end = _as_optional_float(clip_cfg.get("end_s", clip_cfg.get("end")))
    if start is None and end is None:
        return None
    clip_start = max(0.0, float(start or 0.0))
    clip_end = float(end) if end is not None else None
    if clip_end is not None and clip_end <= clip_start:
        raise ValueError(f"Invalid scene clip window: end_s={clip_end} must be greater than start_s={clip_start}")
    return clip_start, clip_end


def _clip_audio_window(
    audio: np.ndarray,
    sample_rate_hz: int,
    clip_bounds: Optional[Tuple[float, Optional[float]]],
) -> np.ndarray:
    if clip_bounds is None:
        return audio
    start_s, end_s = clip_bounds
    start_idx = max(0, int(round(start_s * float(sample_rate_hz))))
    end_idx = int(round(end_s * float(sample_rate_hz))) if end_s is not None else int(audio.shape[0])
    end_idx = max(start_idx, min(int(audio.shape[0]), end_idx))
    return audio[start_idx:end_idx]


def _median(values: Iterable[Optional[float]]) -> Optional[float]:
    cleaned = [float(v) for v in values if v is not None]
    if not cleaned:
        return None
    return float(np.median(np.asarray(cleaned, dtype=np.float64)))


def _percentile_list(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    arr = np.asarray(values, dtype=np.float64)
    return float(np.percentile(arr, pct))


def _as_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _read_jsonl(path: str | Path) -> Iterable[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _extract_output_stats(row: Dict[str, Any]) -> Dict[str, Any] | None:
    for key in ("audio_output", "audio.output.stats", "output", "output_stats"):
        value = row.get(key)
        if isinstance(value, dict):
            return value
    return None


def _as_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    return [tok for tok in text.lower().replace("\n", " ").split(" ") if tok]


def _levenshtein(a: List[str], b: List[str]) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, atok in enumerate(a, start=1):
        cur = [i]
        for j, btok in enumerate(b, start=1):
            cost = 0 if atok == btok else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _wrap_deg(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _scene_labels(scene: Dict[str, Any]) -> Dict[str, Any]:
    labels = scene.get("labels")
    if isinstance(labels, dict):
        return labels
    return scene


def _scene_segments(labels: Dict[str, Any], *keys: str) -> List[Dict[str, Any]]:
    for key in keys:
        value = labels.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for segment in segments:
        bounds = _segment_bounds(segment)
        if bounds is None:
            continue
        start_s, end_s = bounds
        if end_s is not None and end_s < start_s:
            continue
        normalized = dict(segment)
        normalized["_start_s"] = start_s
        normalized["_end_s"] = end_s if end_s is not None else start_s
        out.append(normalized)
    out.sort(key=lambda item: (float(item.get("_start_s", 0.0)), float(item.get("_end_s", 0.0))))
    return out


def _segment_bounds(segment: Dict[str, Any]) -> Optional[Tuple[float, Optional[float]]]:
    start = _time_to_seconds(
        segment.get("start_s", segment.get("start_sec", segment.get("start_ms", segment.get("start_ns", segment.get("start"))))))
    end = _time_to_seconds(
        segment.get("end_s", segment.get("end_sec", segment.get("end_ms", segment.get("end_ns", segment.get("end"))))))
    if start is None and end is None:
        return None
    if start is None:
        start = end if end is not None else 0.0
    return float(start), end if end is None else float(end)


def _time_to_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        raw = float(value)
    except Exception:
        return None
    if raw < 0:
        return None
    if raw >= 1_000_000_000.0:
        return raw / 1_000_000_000.0
    if raw >= 1_000_000.0 and float(int(raw)) == raw:
        return raw / 1_000_000_000.0
    if raw >= 1_000.0 and float(int(raw)) == raw and raw > 1_000_000.0:
        return raw / 1_000.0
    return raw


def _segment_label_id(segment: Dict[str, Any]) -> Optional[str]:
    for key in ("speaker_id", "target_id", "selected_target_id", "track_id", "identity_id", "identity", "label_id"):
        value = segment.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return None


def _segment_track_id(segment: Dict[str, Any]) -> Optional[str]:
    for key in ("track_id", "selected_track_id", "identity_id", "identity", "face_id"):
        value = segment.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return None


def _segment_present(segment: Dict[str, Any]) -> Optional[bool]:
    for key in ("present", "face_present", "visible", "is_present"):
        if key in segment:
            return bool(segment.get(key))
    if "state" in segment:
        state = str(segment.get("state", "") or "").lower()
        if state in {"present", "visible", "on", "true", "active"}:
            return True
        if state in {"absent", "missing", "off", "false", "inactive"}:
            return False
    return None


def _sorted_rows(rows: Iterable[Dict[str, Any]], time_key: str) -> List[Dict[str, Any]]:
    out = [dict(row) for row in rows if isinstance(row, dict)]
    out.sort(key=lambda item: _as_optional_int(item.get(time_key)) or 0)
    return out


def _lock_intervals(lock_rows: List[Dict[str, Any]]) -> List[Tuple[float, float, Dict[str, Any]]]:
    intervals: List[Tuple[float, float, Dict[str, Any]]] = []
    for idx, row in enumerate(lock_rows):
        start = _as_optional_int(row.get("t_ns"))
        if start is None:
            continue
        if idx + 1 < len(lock_rows):
            end = _as_optional_int(lock_rows[idx + 1].get("t_ns"))
        else:
            end = None
        if end is None or end <= start:
            continue
        intervals.append((start / 1_000_000_000.0, end / 1_000_000_000.0, row))
    return intervals


def _overlap_seconds(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _compute_selection_accuracy(lock_rows: List[Dict[str, Any]], speaker_segments: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    if not lock_rows or not speaker_segments:
        return {
            "speaker_selection_accuracy": None,
            "speaker_selection_duration_s": None,
            "speaker_selection_correct_duration_s": None,
        }

    total_s = 0.0
    correct_s = 0.0
    for start_s, end_s, row in _lock_intervals(lock_rows):
        selected = _segment_label_id(row)
        if selected is None:
            continue
        for seg in speaker_segments:
            seg_start = float(seg.get("_start_s", 0.0))
            seg_end = float(seg.get("_end_s", 0.0))
            overlap = _overlap_seconds(start_s, end_s, seg_start, seg_end)
            if overlap <= 0.0:
                continue
            truth = _segment_label_id(seg)
            total_s += overlap
            if truth is not None and selected == truth:
                correct_s += overlap
    return {
        "speaker_selection_accuracy": float(correct_s / total_s) if total_s > 0.0 else None,
        "speaker_selection_duration_s": float(total_s) if total_s > 0.0 else None,
        "speaker_selection_correct_duration_s": float(correct_s) if total_s > 0.0 else None,
    }


def _compute_steering_error(lock_rows: List[Dict[str, Any]], bearing_segments: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    if not lock_rows or not bearing_segments:
        return {
            "steering_mae_deg": None,
            "steering_rmse_deg": None,
            "steering_p95_deg": None,
            "steering_samples": None,
            "steering_duration_s": None,
        }

    errors: List[float] = []
    weights: List[float] = []
    for start_s, end_s, row in _lock_intervals(lock_rows):
        predicted = _as_optional_float(row.get("target_bearing_deg", row.get("angle_deg")))
        if predicted is None:
            continue
        midpoint_s = 0.5 * (start_s + end_s)
        truth_segment = _segment_at_time(bearing_segments, midpoint_s)
        if truth_segment is None:
            continue
        truth = _as_optional_float(truth_segment.get("bearing_deg", truth_segment.get("angle_deg")))
        if truth is None:
            continue
        overlap = max(0.0, end_s - start_s)
        if overlap <= 0.0:
            continue
        error = abs(_wrap_deg(predicted - truth))
        errors.append(float(error))
        weights.append(float(overlap))

    if not errors:
        return {
            "steering_mae_deg": None,
            "steering_rmse_deg": None,
            "steering_p95_deg": None,
            "steering_samples": None,
            "steering_duration_s": None,
        }

    arr = np.asarray(errors, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    duration_s = float(np.sum(w))
    mae = float(np.sum(arr * w) / max(EPS, np.sum(w)))
    rmse = float(np.sqrt(np.sum(np.square(arr) * w) / max(EPS, np.sum(w))))
    return {
        "steering_mae_deg": mae,
        "steering_rmse_deg": rmse,
        "steering_p95_deg": float(np.percentile(arr, 95.0)),
        "steering_samples": float(arr.size),
        "steering_duration_s": duration_s,
    }


def _compute_id_churn(segments: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    if not segments:
        return {
            "id_churn_rate": None,
            "id_switch_count": None,
            "id_track_duration_s": None,
        }

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for segment in segments:
        label = _segment_label_id(segment)
        track = _segment_track_id(segment)
        if label is None or track is None:
            continue
        grouped.setdefault(label, []).append(segment)

    switch_count = 0
    duration_s = 0.0
    for label_segments in grouped.values():
        label_segments.sort(key=lambda item: (float(item.get("_start_s", 0.0)), float(item.get("_end_s", 0.0))))
        prev_track: Optional[str] = None
        for segment in label_segments:
            start_s = float(segment.get("_start_s", 0.0))
            end_s = float(segment.get("_end_s", start_s))
            if end_s > start_s:
                duration_s += end_s - start_s
            track = _segment_track_id(segment)
            if track is None:
                continue
            if prev_track is not None and track != prev_track:
                switch_count += 1
            prev_track = track

    return {
        "id_churn_rate": float(switch_count / max(EPS, duration_s / 60.0)) if duration_s > 0.0 else None,
        "id_switch_count": float(switch_count),
        "id_track_duration_s": float(duration_s),
    }


def _compute_face_reacquire(segments: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    if not segments:
        return {
            "face_reacquire_latency_p50_ms": None,
            "face_reacquire_latency_p95_ms": None,
            "face_reacquire_samples": None,
        }

    latencies_ms: List[float] = []
    prev_present: Optional[bool] = None
    absent_end_s: Optional[float] = None
    for segment in segments:
        present = _segment_present(segment)
        start_s = float(segment.get("_start_s", 0.0))
        end_s = float(segment.get("_end_s", start_s))
        if present is True:
            if prev_present is False and absent_end_s is not None and start_s >= absent_end_s:
                latencies_ms.append((start_s - absent_end_s) * 1000.0)
            prev_present = True
        elif present is False:
            prev_present = False
            absent_end_s = end_s

    if not latencies_ms:
        return {
            "face_reacquire_latency_p50_ms": None,
            "face_reacquire_latency_p95_ms": None,
            "face_reacquire_samples": None,
        }
    arr = np.asarray(latencies_ms, dtype=np.float64)
    return {
        "face_reacquire_latency_p50_ms": float(np.percentile(arr, 50.0)),
        "face_reacquire_latency_p95_ms": float(np.percentile(arr, 95.0)),
        "face_reacquire_samples": float(arr.size),
    }


def _merge_face_reacquire(current: Dict[str, Optional[float]], conversation_summary: Optional[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    if not isinstance(conversation_summary, dict):
        return current
    out = dict(current)
    if out.get("face_reacquire_latency_p50_ms") is None:
        out["face_reacquire_latency_p50_ms"] = _as_optional_float(conversation_summary.get("face_reacquire_latency_p50_ms"))
    if out.get("face_reacquire_latency_p95_ms") is None:
        out["face_reacquire_latency_p95_ms"] = _as_optional_float(conversation_summary.get("face_reacquire_latency_p95_ms"))
    if out.get("face_reacquire_samples") is None:
        out["face_reacquire_samples"] = _as_optional_float(conversation_summary.get("face_reacquire_samples"))
    return out


def _segment_at_time(segments: List[Dict[str, Any]], t_s: float) -> Optional[Dict[str, Any]]:
    candidate: Optional[Dict[str, Any]] = None
    for segment in segments:
        start_s = float(segment.get("_start_s", 0.0))
        end_s = float(segment.get("_end_s", start_s))
        if start_s <= t_s <= end_s:
            if candidate is None or start_s >= float(candidate.get("_start_s", 0.0)):
                candidate = segment
    return candidate

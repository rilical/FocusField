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
        return {"samples": float(len(bearings)), "mae_step_deg": None, "std_step_deg": None}

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
    baseline_mono = as_mono(baseline_audio)
    candidate_mono = as_mono(candidate_audio)

    ref_target_path = scene.get("target_reference_wav")
    ref_noise_path = scene.get("interferer_reference_wav")

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
        ref_target_mono = as_mono(ref_target)
        si_sdr_base = si_sdr_db(ref_target_mono, baseline_mono)
        si_sdr_cand = si_sdr_db(ref_target_mono, candidate_mono)
        stoi_base = stoi_proxy(ref_target_mono, baseline_mono, baseline_sr)
        stoi_cand = stoi_proxy(ref_target_mono, candidate_mono, baseline_sr)

        if ref_noise_path:
            noise_sr, ref_noise = load_wav(ref_noise_path)
            if noise_sr != baseline_sr:
                raise ValueError(f"Interference sample rate mismatch in scene={scene_id}")
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

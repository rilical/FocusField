#!/usr/bin/env python3
"""Pi runtime performance gate for FocusField artifacts."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from focusfield.bench.profile_loader import default_pi_nightly_profile_path, load_pi_perf_gate_thresholds


def _load_jsonl(path: Path) -> Iterable[Dict[str, object]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    p = min(100.0, max(0.0, float(pct)))
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(ordered[lo])
    ratio = rank - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * ratio)


def _as_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return int(default)


def _resolve_run_dir(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if path.is_file():
        ref = path.read_text(encoding="utf-8").strip()
        if ref:
            return (path.parent / ref).resolve()
    return path.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate FocusField Pi run performance from artifacts.")
    parser.add_argument("--run-dir", default="artifacts/LATEST", help="Run directory containing logs/*.jsonl")
    parser.add_argument(
        "--profile",
        default=str(default_pi_nightly_profile_path()),
        help="Benchmark profile YAML path (shared with focusbench_ab).",
    )
    parser.add_argument("--latency-p95-max", type=float, default=None)
    parser.add_argument("--latency-p99-max", type=float, default=None)
    parser.add_argument("--overflow-delta-max", type=int, default=None)
    parser.add_argument("--queue-full-max", type=int, default=None)
    parser.add_argument("--no-candidates-ratio-max", type=float, default=None)
    parser.add_argument("--speech-with-no-lock-ratio-max", type=float, default=None)
    parser.add_argument("--no-faces-fallback-ratio-max", type=float, default=None)
    parser.add_argument("--no-faces-audio-fallback-ratio-max", type=float, default=None)
    parser.add_argument("--overflow-rate-max-per-min", type=float, default=None)
    parser.add_argument("--face-track-rate-min", type=float, default=None)
    parser.add_argument("--face-detection-stall-max-ms", type=float, default=None)
    parser.add_argument("--lock-continuity-ratio-min", type=float, default=None)
    parser.add_argument("--min-runtime-seconds", type=float, default=None)
    parser.add_argument("--no-candidates-denominator-min", type=int, default=None)
    args = parser.parse_args()

    profile_thresholds = load_pi_perf_gate_thresholds(args.profile)

    latency_p95_max = float(args.latency_p95_max) if args.latency_p95_max is not None else float(profile_thresholds["latency_p95_max"])
    latency_p99_max = float(args.latency_p99_max) if args.latency_p99_max is not None else float(profile_thresholds["latency_p99_max"])
    overflow_delta_max = int(args.overflow_delta_max) if args.overflow_delta_max is not None else int(profile_thresholds["overflow_delta_max"])
    queue_full_max = int(args.queue_full_max) if args.queue_full_max is not None else int(profile_thresholds["queue_full_max"])
    no_candidates_ratio_max = (
        float(args.no_candidates_ratio_max)
        if args.no_candidates_ratio_max is not None
        else float(profile_thresholds["no_candidates_ratio_max"])
    )
    speech_with_no_lock_ratio_max = (
        float(args.speech_with_no_lock_ratio_max)
        if args.speech_with_no_lock_ratio_max is not None
        else float(profile_thresholds["speech_with_no_lock_ratio_max"])
    )
    no_faces_fallback_ratio_max = (
        float(args.no_faces_fallback_ratio_max)
        if args.no_faces_fallback_ratio_max is not None
        else float(profile_thresholds["no_faces_fallback_ratio_max"])
    )
    overflow_rate_max_per_min = (
        float(args.overflow_rate_max_per_min)
        if args.overflow_rate_max_per_min is not None
        else float(profile_thresholds["overflow_rate_max_per_min"])
    )
    face_track_rate_min = (
        float(args.face_track_rate_min)
        if args.face_track_rate_min is not None
        else float(profile_thresholds["face_track_rate_min"])
    )
    face_detection_stall_max_ms = (
        float(args.face_detection_stall_max_ms)
        if args.face_detection_stall_max_ms is not None
        else float(profile_thresholds["face_detection_stall_max_ms"])
    )
    lock_continuity_ratio_min = (
        float(args.lock_continuity_ratio_min)
        if args.lock_continuity_ratio_min is not None
        else float(profile_thresholds["lock_continuity_ratio_min"])
    )
    min_runtime_seconds = (
        float(args.min_runtime_seconds)
        if args.min_runtime_seconds is not None
        else float(profile_thresholds["min_runtime_seconds"])
    )
    no_candidates_denominator_min = (
        int(args.no_candidates_denominator_min)
        if args.no_candidates_denominator_min is not None
        else int(profile_thresholds["no_candidates_denominator_min"])
    )

    no_faces_ratio_threshold = (
        float(args.no_faces_audio_fallback_ratio_max)
        if args.no_faces_audio_fallback_ratio_max is not None
        else no_faces_fallback_ratio_max
    )

    run_dir = _resolve_run_dir(args.run_dir)
    perf_path = run_dir / "logs" / "perf.jsonl"
    events_path = run_dir / "logs" / "events.jsonl"

    if not perf_path.exists():
        print(f"FAIL missing perf log: {perf_path}")
        return 2
    if not events_path.exists():
        print(f"FAIL missing events log: {events_path}")
        return 2

    latencies: List[float] = []
    overflow_first: Optional[int] = None
    overflow_last: Optional[int] = None
    perf_first_t_ns: Optional[int] = None
    perf_last_t_ns: Optional[int] = None
    fusion_candidates_published = 0
    worker_first: Dict[str, tuple[int, int]] = {}
    worker_last: Dict[str, tuple[int, int]] = {}
    worker_prev_processed: Dict[str, int] = {}
    worker_last_progress_t_ns: Dict[str, int] = {}
    worker_max_stall_ns: Dict[str, int] = {}

    for entry in _load_jsonl(perf_path):
        t_ns = _as_int(entry.get("t_ns"), default=0)
        if t_ns > 0:
            if perf_first_t_ns is None:
                perf_first_t_ns = t_ns
            perf_last_t_ns = t_ns
        enhanced = entry.get("enhanced_final")
        if isinstance(enhanced, dict):
            latency = _as_float(
                enhanced.get(
                    "pipeline_queue_age_ms",
                    enhanced.get("last_latency_ms"),
                )
            )
            if latency is not None:
                latencies.append(latency)
        audio_capture = entry.get("audio_capture")
        if isinstance(audio_capture, dict):
            overflow = _as_int(
                audio_capture.get("status_input_overflow_total", audio_capture.get("status_input_overflow")),
                default=0,
            )
            if overflow_first is None:
                overflow_first = overflow
            overflow_last = overflow
        bus_summary = entry.get("bus")
        if isinstance(bus_summary, dict):
            publish_delta = bus_summary.get("publish_delta")
            if isinstance(publish_delta, dict):
                fusion_candidates_published += max(0, _as_int(publish_delta.get("fusion.candidates"), default=0))
        workers = entry.get("worker_loops")
        if isinstance(workers, dict):
            for module, worker_stats in workers.items():
                module_name = str(module or "")
                if not module_name.startswith("vision.face_track."):
                    continue
                if not isinstance(worker_stats, dict):
                    continue
                worker_t_ns = _as_int(worker_stats.get("t_ns"), default=t_ns)
                processed_cycles = _as_int(worker_stats.get("processed_cycles"), default=0)
                if module_name not in worker_first:
                    worker_first[module_name] = (worker_t_ns, processed_cycles)
                    worker_last_progress_t_ns[module_name] = worker_t_ns
                    worker_max_stall_ns[module_name] = 0
                prev_processed = worker_prev_processed.get(module_name, processed_cycles)
                last_progress_t_ns = worker_last_progress_t_ns.get(module_name, worker_t_ns)
                if processed_cycles > prev_processed:
                    gap_ns = max(0, worker_t_ns - last_progress_t_ns)
                    prev_max = worker_max_stall_ns.get(module_name, 0)
                    if gap_ns > prev_max:
                        worker_max_stall_ns[module_name] = gap_ns
                    worker_last_progress_t_ns[module_name] = worker_t_ns
                worker_prev_processed[module_name] = processed_cycles
                worker_last[module_name] = (worker_t_ns, processed_cycles)

    queue_full_audio = 0
    queue_full_vision = 0
    queue_full_by_topic: Dict[str, int] = {}
    no_candidates_count = 0
    speech_with_no_lock_count = 0
    no_candidates_by_reason: Counter[str] = Counter()
    for entry in _load_jsonl(events_path):
        context = entry.get("context")
        if not isinstance(context, dict):
            continue
        module = str(context.get("module", "") or "")
        event = str(context.get("event", "") or "")
        details = context.get("details")
        if not isinstance(details, dict):
            details = {}

        if module == "core.bus" and event == "queue_full":
            topic = str(details.get("topic", "") or "")
            queue_full_by_topic[topic] = queue_full_by_topic.get(topic, 0) + 1
            if topic == "audio.frames":
                queue_full_audio += 1
            if topic.startswith("vision.frames."):
                queue_full_vision += 1
        if module == "fusion.av_association" and event == "no_candidates":
            no_candidates_count += 1
            reason = str(details.get("reason", "unknown") or "unknown")
            no_candidates_by_reason[reason] += 1
            if bool(details.get("vad_speech", False)):
                speech_with_no_lock_count += 1

    p50 = _percentile(latencies, 50.0)
    p95 = _percentile(latencies, 95.0)
    p99 = _percentile(latencies, 99.0)
    overflow_delta = max(0, (overflow_last or 0) - (overflow_first or 0))
    duration_min = 0.0
    duration_s = 0.0
    if perf_first_t_ns is not None and perf_last_t_ns is not None and perf_last_t_ns > perf_first_t_ns:
        duration_s = (perf_last_t_ns - perf_first_t_ns) / 1_000_000_000.0
        duration_min = duration_s / 60.0
    overflow_rate_per_min = (overflow_delta / duration_min) if duration_min > 0.0 else float(overflow_delta)
    vision_queue_full_total = queue_full_vision
    speech_with_no_lock_ratio = float(speech_with_no_lock_count) / float(max(1, no_candidates_count))
    lock_continuity_ratio = max(0.0, min(1.0, 1.0 - speech_with_no_lock_ratio))
    no_faces_fallback_ratio = float(no_candidates_by_reason.get("no_faces_audio_fallback", 0)) / float(max(1, no_candidates_count))

    no_candidates_ratio: Optional[float]
    insufficient_reasons: List[str] = []
    if duration_s < min_runtime_seconds:
        insufficient_reasons.append(
            f"runtime too short: {duration_s:.1f}s < min_runtime_seconds {min_runtime_seconds:.1f}s"
        )
    if len(latencies) == 0:
        insufficient_reasons.append("no latency samples found in perf.jsonl")
    if fusion_candidates_published < no_candidates_denominator_min:
        no_candidates_ratio = None
        insufficient_reasons.append(
            "insufficient fusion candidate denominator: "
            f"{fusion_candidates_published} < {no_candidates_denominator_min}"
        )
    else:
        no_candidates_ratio = float(no_candidates_count) / float(max(1, fusion_candidates_published))

    face_worker_rates: Dict[str, float] = {}
    for module_name, first_values in worker_first.items():
        if module_name not in worker_last:
            continue
        first_t_ns, first_processed = first_values
        last_t_ns, last_processed = worker_last[module_name]
        dt_s = (last_t_ns - first_t_ns) / 1_000_000_000.0
        if dt_s <= 0.0:
            face_worker_rates[module_name] = 0.0
            continue
        d_processed = max(0, last_processed - first_processed)
        face_worker_rates[module_name] = float(d_processed / dt_s)
    face_track_rate_min_actual = min(face_worker_rates.values()) if face_worker_rates else 0.0
    face_detection_stall_max_ms_actual = (
        max(float(value) for value in worker_max_stall_ns.values()) / 1_000_000.0 if worker_max_stall_ns else 0.0
    )

    failures: List[str] = []
    if p95 is None or p99 is None:
        failures.append("latency samples missing")
    else:
        if p95 > latency_p95_max:
            failures.append(f"latency p95 {p95:.1f}ms > {latency_p95_max:.1f}ms")
        if p99 > latency_p99_max:
            failures.append(f"latency p99 {p99:.1f}ms > {latency_p99_max:.1f}ms")
    if overflow_delta > overflow_delta_max:
        failures.append(f"status_input_overflow delta {overflow_delta} > {overflow_delta_max}")
    if queue_full_audio > queue_full_max:
        failures.append(f"queue_full audio.frames {queue_full_audio} > {queue_full_max}")
    if vision_queue_full_total > queue_full_max:
        failures.append(f"queue_full vision.frames.* {vision_queue_full_total} > {queue_full_max}")
    if no_candidates_ratio is not None and no_candidates_ratio > no_candidates_ratio_max:
        failures.append(f"no_candidates ratio {no_candidates_ratio:.3f} > {no_candidates_ratio_max:.3f}")
    if speech_with_no_lock_ratio > speech_with_no_lock_ratio_max:
        failures.append(f"speech_with_no_lock ratio {speech_with_no_lock_ratio:.3f} > {speech_with_no_lock_ratio_max:.3f}")
    if lock_continuity_ratio < lock_continuity_ratio_min:
        failures.append(f"lock_continuity ratio {lock_continuity_ratio:.3f} < {lock_continuity_ratio_min:.3f}")
    if no_faces_fallback_ratio > no_faces_ratio_threshold:
        failures.append(f"no_faces_audio_fallback ratio {no_faces_fallback_ratio:.3f} > {no_faces_ratio_threshold:.3f}")
    if overflow_rate_per_min > overflow_rate_max_per_min:
        failures.append(f"overflow rate {overflow_rate_per_min:.2f}/min > {overflow_rate_max_per_min:.2f}/min")
    if face_track_rate_min_actual < face_track_rate_min:
        failures.append(f"face_track_rate_min {face_track_rate_min_actual:.3f} < {face_track_rate_min:.3f}")
    if face_detection_stall_max_ms_actual > face_detection_stall_max_ms:
        failures.append(
            f"face_detection_stall_max_ms {face_detection_stall_max_ms_actual:.1f} > {face_detection_stall_max_ms:.1f}"
        )

    print(f"run_dir={run_dir}")
    print(f"profile={args.profile}")
    if p50 is not None and p95 is not None and p99 is not None:
        print(f"latency_ms p50={p50:.1f} p95={p95:.1f} p99={p99:.1f} samples={len(latencies)}")
    else:
        print("latency_ms unavailable")
    print(f"runtime_seconds={duration_s:.1f}")
    print(f"status_input_overflow delta={overflow_delta} first={overflow_first or 0} last={overflow_last or 0}")
    print(f"status_input_overflow rate_per_min={overflow_rate_per_min:.2f} duration_min={duration_min:.2f}")
    print(f"queue_full audio.frames={queue_full_audio} vision.frames.*={vision_queue_full_total}")
    print(
        "fusion no_candidates_count="
        f"{no_candidates_count} fusion_candidates_published={fusion_candidates_published} "
        f"ratio={'n/a' if no_candidates_ratio is None else f'{no_candidates_ratio:.3f}'}"
    )
    print(
        f"speech_with_no_lock_count={speech_with_no_lock_count} "
        f"speech_with_no_lock_ratio={speech_with_no_lock_ratio:.3f} "
        f"lock_continuity_ratio={lock_continuity_ratio:.3f}"
    )
    if no_candidates_by_reason:
        distribution = ",".join(f"{key}:{count}" for key, count in sorted(no_candidates_by_reason.items()))
        print(f"no_candidates_reasons={distribution}")
    print(
        f"no_faces_audio_fallback_ratio={no_faces_fallback_ratio:.3f} "
        f"threshold={no_faces_ratio_threshold:.3f}"
    )
    print(f"face_track_rate_min={face_track_rate_min_actual:.3f} worker_count={len(face_worker_rates)}")
    if face_worker_rates:
        ordered_workers = sorted(face_worker_rates.items(), key=lambda item: item[0])
        print("face_track_rates=" + ",".join(f"{module}:{rate:.3f}" for module, rate in ordered_workers))
    print(f"face_detection_stall_max_ms={face_detection_stall_max_ms_actual:.1f}")
    if queue_full_by_topic:
        busiest = sorted(queue_full_by_topic.items(), key=lambda item: item[1], reverse=True)[:8]
        print("queue_full_top=" + ",".join(f"{topic}:{count}" for topic, count in busiest))

    if insufficient_reasons:
        print("RESULT=INSUFFICIENT_DATA")
        for reason in insufficient_reasons:
            print(f"- {reason}")
        return 3

    if failures:
        print("RESULT=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("RESULT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

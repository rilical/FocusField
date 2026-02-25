#!/usr/bin/env python3
"""Pi runtime performance gate for FocusField artifacts."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate FocusField Pi run performance from artifacts.")
    parser.add_argument("--run-dir", default="artifacts/LATEST", help="Run directory containing logs/*.jsonl")
    parser.add_argument("--latency-p95-max", type=float, default=350.0)
    parser.add_argument("--latency-p99-max", type=float, default=550.0)
    parser.add_argument("--overflow-delta-max", type=int, default=5)
    parser.add_argument("--queue-full-max", type=int, default=5)
    parser.add_argument("--no-candidates-ratio-max", type=float, default=0.65)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).expanduser()
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
    fusion_candidates_published = 0

    for entry in _load_jsonl(perf_path):
        enhanced = entry.get("enhanced_final")
        if isinstance(enhanced, dict):
            latency = _as_float(enhanced.get("last_latency_ms"))
            if latency is not None:
                latencies.append(latency)
        audio_capture = entry.get("audio_capture")
        if isinstance(audio_capture, dict):
            overflow = _as_int(audio_capture.get("status_input_overflow"), default=0)
            if overflow_first is None:
                overflow_first = overflow
            overflow_last = overflow
        bus_summary = entry.get("bus")
        if isinstance(bus_summary, dict):
            publish_delta = bus_summary.get("publish_delta")
            if isinstance(publish_delta, dict):
                fusion_candidates_published += max(0, _as_int(publish_delta.get("fusion.candidates"), default=0))

    queue_full_audio = 0
    queue_full_vision = 0
    queue_full_by_topic: Dict[str, int] = {}
    no_candidates_count = 0
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

    p50 = _percentile(latencies, 50.0)
    p95 = _percentile(latencies, 95.0)
    p99 = _percentile(latencies, 99.0)
    overflow_delta = max(0, (overflow_last or 0) - (overflow_first or 0))
    vision_queue_full_total = queue_full_vision
    no_candidates_ratio = (
        float(no_candidates_count) / float(max(1, fusion_candidates_published))
        if fusion_candidates_published > 0
        else 1.0
    )

    failures: List[str] = []
    if p95 is None or p99 is None:
        failures.append("latency samples missing")
    else:
        if p95 > float(args.latency_p95_max):
            failures.append(f"latency p95 {p95:.1f}ms > {args.latency_p95_max:.1f}ms")
        if p99 > float(args.latency_p99_max):
            failures.append(f"latency p99 {p99:.1f}ms > {args.latency_p99_max:.1f}ms")
    if overflow_delta > int(args.overflow_delta_max):
        failures.append(f"status_input_overflow delta {overflow_delta} > {args.overflow_delta_max}")
    if queue_full_audio > int(args.queue_full_max):
        failures.append(f"queue_full audio.frames {queue_full_audio} > {args.queue_full_max}")
    if vision_queue_full_total > int(args.queue_full_max):
        failures.append(f"queue_full vision.frames.* {vision_queue_full_total} > {args.queue_full_max}")
    if no_candidates_ratio > float(args.no_candidates_ratio_max):
        failures.append(
            f"no_candidates ratio {no_candidates_ratio:.3f} > {float(args.no_candidates_ratio_max):.3f}"
        )

    print(f"run_dir={run_dir}")
    if p50 is not None and p95 is not None and p99 is not None:
        print(f"latency_ms p50={p50:.1f} p95={p95:.1f} p99={p99:.1f} samples={len(latencies)}")
    else:
        print("latency_ms unavailable")
    print(f"status_input_overflow delta={overflow_delta} first={overflow_first or 0} last={overflow_last or 0}")
    print(f"queue_full audio.frames={queue_full_audio} vision.frames.*={vision_queue_full_total}")
    print(
        "fusion no_candidates_count="
        f"{no_candidates_count} fusion_candidates_published={fusion_candidates_published} "
        f"ratio={no_candidates_ratio:.3f}"
    )
    if queue_full_by_topic:
        busiest = sorted(queue_full_by_topic.items(), key=lambda item: item[1], reverse=True)[:8]
        print("queue_full_top=" + ",".join(f"{topic}:{count}" for topic, count in busiest))

    if failures:
        print("RESULT=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("RESULT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

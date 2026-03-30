#!/usr/bin/env python3
"""Poll a FocusField telemetry endpoint and persist a compact watch log."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict


def _load_json(url: str, timeout_s: float) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as response:
        payload = json.load(response)
    return payload if isinstance(payload, dict) else {}


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://192.168.1.232:8080/telemetry")
    parser.add_argument("--duration-s", type=float, default=1800.0)
    parser.add_argument("--period-s", type=float, default=2.0)
    parser.add_argument("--timeout-s", type=float, default=5.0)
    parser.add_argument(
        "--out-dir",
        default="artifacts/pi_watch",
        help="Directory for NDJSON samples and the summary JSON.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"pi_watch_{stamp}.ndjson"
    summary_path = out_dir / f"pi_watch_{stamp}_summary.json"

    start = time.time()
    deadline = start + max(0.0, float(args.duration_s))

    samples = 0
    errors = 0
    face_positive_samples = 0
    fallback_samples = 0
    max_face_count = 0
    queue_ages: list[float] = []
    face_counts: list[int] = []
    status_counts: dict[str, int] = {}

    with out_path.open("w", encoding="utf-8") as handle:
        while time.time() < deadline:
            tick = time.time()
            try:
                data = _load_json(args.url, timeout_s=float(args.timeout_s))
                perf = data.get("perf_summary") or {}
                enhanced = perf.get("enhanced_final") if isinstance(perf, dict) else {}
                if not isinstance(enhanced, dict):
                    enhanced = {}

                face_count = len(data.get("face_summaries") or [])
                queue_age_ms = enhanced.get("pipeline_queue_age_ms", enhanced.get("last_latency_ms"))
                if isinstance(queue_age_ms, (int, float)):
                    queue_ages.append(float(queue_age_ms))

                face_counts.append(face_count)
                max_face_count = max(max_face_count, face_count)
                if face_count > 0:
                    face_positive_samples += 1

                audio_fallback_active = bool(data.get("audio_fallback_active"))
                if audio_fallback_active:
                    fallback_samples += 1

                status = str((data.get("health_summary") or {}).get("status") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

                record = {
                    "ts": tick,
                    "seq": data.get("seq"),
                    "health": status,
                    "detector_backend_active": data.get("detector_backend_active"),
                    "active_face_cameras": (data.get("meta") or {}).get("active_face_cameras"),
                    "face_count": face_count,
                    "audio_fallback_active": audio_fallback_active,
                    "queue_age_ms": queue_age_ms,
                    "capture_overflow_window": data.get("capture_overflow_window"),
                    "bus_drop_counts_window": data.get("bus_drop_counts_window"),
                    "mic_health_summary": data.get("mic_health_summary"),
                    "lock_state": data.get("lock_state"),
                }
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
                handle.flush()
                samples += 1
            except Exception as exc:  # noqa: BLE001
                handle.write(json.dumps({"ts": tick, "error": str(exc)}, separators=(",", ":")) + "\n")
                handle.flush()
                errors += 1

            sleep_s = float(args.period_s) - (time.time() - tick)
            if sleep_s > 0:
                time.sleep(sleep_s)

    ended = time.time()
    summary = {
        "started_at_s": start,
        "ended_at_s": ended,
        "duration_s": ended - start,
        "period_s": float(args.period_s),
        "samples": samples,
        "errors": errors,
        "face_positive_ratio": (face_positive_samples / samples) if samples else None,
        "audio_fallback_ratio": (fallback_samples / samples) if samples else None,
        "avg_face_count": _mean([float(v) for v in face_counts]),
        "max_face_count": max_face_count,
        "queue_age_ms": {
            "avg": _mean(queue_ages),
            "max": max(queue_ages) if queue_ages else None,
        },
        "status_counts": status_counts,
        "ndjson_path": str(out_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

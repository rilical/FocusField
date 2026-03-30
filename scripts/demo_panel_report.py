#!/usr/bin/env python3
"""Generate an engineering-facing demo panel packet from benchmark artifacts."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def build_demo_panel_report(
    bench_report_path: str,
    *,
    demo_readiness_path: Optional[str] = None,
    ab_clip_path: Optional[str] = None,
) -> Dict[str, Any]:
    bench_path = Path(bench_report_path).expanduser().resolve()
    bench_report = _load_json(bench_path)
    demo_path = Path(demo_readiness_path).expanduser().resolve() if demo_readiness_path else None
    demo_report = _load_json(demo_path) if demo_path is not None else {}
    ab_clip = Path(ab_clip_path).expanduser().resolve() if ab_clip_path else None

    quality = _dict_at(bench_report, "summary", "quality")
    latency = _dict_at(bench_report, "summary", "latency")
    runtime = _dict_at(bench_report, "summary", "runtime")
    gates = _dict_at(bench_report, "summary", "gates")
    readiness_summary = demo_report.get("summary", {}) if isinstance(demo_report, dict) else {}

    clarity = {
        "si_sdr_delta_db": _opt_float(quality.get("median_si_sdr_delta_db")),
        "stoi_delta": _opt_float(quality.get("median_stoi_delta")),
        "wer_relative_improvement": _opt_float(quality.get("median_wer_relative_improvement")),
        "sir_delta_db": _opt_float(quality.get("median_sir_delta_db")),
    }
    runtime_summary = {
        "latency_p50_ms": _opt_float(latency.get("p50_ms")),
        "latency_p95_ms": _opt_float(latency.get("p95_ms")),
        "latency_p99_ms": _opt_float(latency.get("p99_ms")),
        "output_underrun_rate": _opt_float(runtime.get("output_underrun_rate")),
        "output_underrun_total": _opt_float(runtime.get("output_underrun_total")),
        "queue_pressure_peak": _opt_float(runtime.get("queue_pressure_peak")),
    }
    meeting_path = {
        "boot_to_host_visible_mic_s": _opt_float(readiness_summary.get("boot_to_host_visible_mic_s")),
        "reconnect_time_s": _opt_float(readiness_summary.get("reconnect_time_s")),
        "zoom_selected_input_device": str(readiness_summary.get("zoom_selected_input_device", "") or ""),
        "crash_free_soak": bool(_dict_get(readiness_summary, "soak", "passed", default=False)),
        "soak_duration_s": _opt_float(_dict_get(readiness_summary, "soak", "duration_s")),
    }
    measurement_notes = [
        "Baseline is the MacBook built-in microphone captured in the same session as the FocusField candidate.",
        "Reference speech uses a close-talk microphone for quality scoring.",
        "Latency claims are internal pipeline measurements, not network or end-to-end meeting round-trip latency.",
        "Meeting-path readiness covers boot-to-host-visible mic, reconnect recovery, and Zoom device-selection proof.",
    ]

    return {
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "bench_report": str(bench_path),
            "demo_readiness": str(demo_path) if demo_path is not None else "",
            "ab_clip": str(ab_clip) if ab_clip is not None else "",
        },
        "verdicts": {
            "bench_passed": bool(gates.get("passed", False)),
            "demo_ready": bool(demo_report.get("passed", False)) if isinstance(demo_report, dict) else False,
        },
        "scorecard": {
            "clarity": clarity,
            "runtime": runtime_summary,
            "meeting_path": meeting_path,
        },
        "plots": bench_report.get("plots", {}),
        "measurement_notes": measurement_notes,
        "bench_report": bench_report,
        "demo_readiness": demo_report,
    }


def write_demo_panel_report(payload: Dict[str, Any], output_dir: str | Path) -> Dict[str, Path]:
    out_dir = Path(output_dir).expanduser().resolve()
    plots_dir = out_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)
    plots_dir.mkdir(parents=True, exist_ok=True)

    copied_plots = _copy_plots(payload.get("plots", {}), plots_dir)
    payload = dict(payload)
    payload["plots"] = copied_plots

    json_path = out_dir / "panel_scorecard.json"
    md_path = out_dir / "panel_scorecard.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    return {
        "json_path": json_path,
        "markdown_path": md_path,
        "plots_dir": plots_dir,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a demo panel packet from FocusBench artifacts")
    parser.add_argument("--bench-report", required=True, help="BenchReport.json path")
    parser.add_argument("--demo-readiness", default="", help="demo_readiness.json path")
    parser.add_argument("--ab-clip", default="", help="Optional short A/B clip path")
    parser.add_argument("--output-dir", required=True, help="Folder for scorecard and copied plots")
    args = parser.parse_args(argv)

    payload = build_demo_panel_report(
        args.bench_report,
        demo_readiness_path=args.demo_readiness or None,
        ab_clip_path=args.ab_clip or None,
    )
    written = write_demo_panel_report(payload, args.output_dir)
    print(json.dumps({key: str(value) for key, value in written.items()}, indent=2, sort_keys=True))
    verdicts = payload.get("verdicts", {})
    return 0 if bool(verdicts.get("bench_passed", False)) and bool(verdicts.get("demo_ready", False)) else 2


def _copy_plots(raw_plots: Any, plots_dir: Path) -> Dict[str, str]:
    if not isinstance(raw_plots, dict):
        return {}
    copied: Dict[str, str] = {}
    for name, raw_path in raw_plots.items():
        src = Path(str(raw_path)).expanduser()
        if not src.exists():
            continue
        target = plots_dir / src.name
        shutil.copy2(src, target)
        copied[str(name)] = str(target)
    return copied


def _render_markdown(payload: Dict[str, Any]) -> str:
    scorecard = payload.get("scorecard", {})
    clarity = scorecard.get("clarity", {}) if isinstance(scorecard, dict) else {}
    runtime = scorecard.get("runtime", {}) if isinstance(scorecard, dict) else {}
    meeting_path = scorecard.get("meeting_path", {}) if isinstance(scorecard, dict) else {}
    verdicts = payload.get("verdicts", {}) if isinstance(payload.get("verdicts"), dict) else {}
    notes = payload.get("measurement_notes", [])
    plots = payload.get("plots", {}) if isinstance(payload.get("plots"), dict) else {}

    lines = [
        "# FocusField Demo Panel Scorecard",
        "",
        f"- Bench verdict: {'PASS' if verdicts.get('bench_passed') else 'FAIL'}",
        f"- Demo readiness: {'PASS' if verdicts.get('demo_ready') else 'FAIL'}",
        "",
        "## Clarity",
        f"- SI-SDR delta (median): {_fmt_float(clarity.get('si_sdr_delta_db'), 'dB')}",
        f"- STOI delta (median): {_fmt_float(clarity.get('stoi_delta'))}",
        f"- WER relative improvement (median): {_fmt_float(clarity.get('wer_relative_improvement'))}",
        f"- SIR delta (median): {_fmt_float(clarity.get('sir_delta_db'), 'dB')}",
        "",
        "## Runtime",
        f"- Latency p50: {_fmt_float(runtime.get('latency_p50_ms'), 'ms')}",
        f"- Latency p95: {_fmt_float(runtime.get('latency_p95_ms'), 'ms')}",
        f"- Latency p99: {_fmt_float(runtime.get('latency_p99_ms'), 'ms')}",
        f"- Output underrun rate: {_fmt_float(runtime.get('output_underrun_rate'))}",
        f"- Queue pressure peak: {_fmt_float(runtime.get('queue_pressure_peak'))}",
        "",
        "## Meeting Path",
        f"- Boot to host-visible mic: {_fmt_float(meeting_path.get('boot_to_host_visible_mic_s'), 's')}",
        f"- Reconnect time: {_fmt_float(meeting_path.get('reconnect_time_s'), 's')}",
        f"- Zoom selected input: {meeting_path.get('zoom_selected_input_device') or 'n/a'}",
        f"- Crash-free soak: {'yes' if meeting_path.get('crash_free_soak') else 'no'}",
        f"- Soak duration: {_fmt_float(meeting_path.get('soak_duration_s'), 's')}",
        "",
        "## Measurement Notes",
    ]
    for note in notes if isinstance(notes, list) else []:
        lines.append(f"- {note}")
    if plots:
        lines.extend(["", "## Plots"])
        for name, path in sorted(plots.items()):
            lines.append(f"- {name}: {path}")
    return "\n".join(lines) + "\n"


def _dict_at(payload: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _dict_get(payload: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return current if current is not None else default


def _load_json(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_float(value: Any, unit: str = "") -> str:
    number = _opt_float(value)
    if number is None:
        return "n/a"
    suffix = f" {unit}" if unit else ""
    return f"{number:.3f}{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())

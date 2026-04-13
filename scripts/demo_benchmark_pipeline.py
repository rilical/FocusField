#!/usr/bin/env python3
"""Run the same-session demo benchmark pipeline end to end."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from focusfield.bench.focusbench import run_focusbench
from focusfield.bench.profile_loader import default_pi_nightly_profile_path, load_focusbench_thresholds
from focusfield.core.config import load_config
from scripts.demo_ab_capture import build_demo_capture_bundle, write_demo_capture_bundle
from scripts.demo_panel_report import build_demo_panel_report, write_demo_panel_report
from scripts.support_bundle import resolve_run_dir


def run_demo_benchmark_pipeline(
    *,
    candidate_run: str,
    baseline_audio_path: str,
    reference_audio_path: str,
    output_dir: str,
    config_path: str = "configs/meeting_peripheral_demo_safe.yaml",
    profile_path: str | Path = default_pi_nightly_profile_path(),
    scene_spec_path: Optional[str] = None,
    video_paths: Optional[Sequence[str]] = None,
    room_profile: str = "noisy_office_room",
    meeting_app: str = "Zoom",
    baseline_device_name: str = "MacBook Built-in Microphone",
    reference_device_name: str = "Close-talk Reference Mic",
    candidate_device_name: str = "FocusField USB Mic",
    sync_marker_time_s: Optional[float] = None,
    demo_readiness_path: Optional[str] = None,
    strict_truth: bool = False,
) -> Dict[str, Any]:
    run_path = resolve_run_dir(candidate_run)
    out_dir = Path(output_dir).expanduser().resolve()
    capture_dir = out_dir / "ab_bundle"
    bench_dir = out_dir / "focusbench"
    panel_dir = out_dir / "panel_packet"

    capture_payload = build_demo_capture_bundle(
        candidate_run=str(run_path),
        baseline_audio_path=baseline_audio_path,
        reference_audio_path=reference_audio_path,
        output_dir=str(capture_dir),
        scene_spec_path=scene_spec_path,
        video_paths=video_paths,
        room_profile=room_profile,
        meeting_app=meeting_app,
        baseline_device_name=baseline_device_name,
        reference_device_name=reference_device_name,
        candidate_device_name=candidate_device_name,
        sync_marker_time_s=sync_marker_time_s,
        dry_run=False,
    )
    written_capture = write_demo_capture_bundle(capture_payload, capture_dir)

    thresholds = _bench_thresholds(load_config(config_path))
    thresholds.update(load_focusbench_thresholds(profile_path))
    bench_report = run_focusbench(
        baseline_run=str(run_path),
        candidate_run=str(run_path),
        scene_manifest=str(written_capture["manifest_path"]),
        output_dir=str(bench_dir),
        thresholds=thresholds,
        strict_truth=bool(strict_truth),
    )

    panel_payload = build_demo_panel_report(
        str(bench_report["output_path"]),
        demo_readiness_path=demo_readiness_path,
    )
    written_panel = write_demo_panel_report(panel_payload, panel_dir)

    gates = bench_report.get("summary", {}).get("gates", {})
    bench_passed = bool(gates.get("passed", False)) if isinstance(gates, dict) else False
    demo_ready = bool(panel_payload.get("verdicts", {}).get("demo_ready", False)) if demo_readiness_path else None
    overall_passed = bench_passed if demo_ready is None else bool(bench_passed and demo_ready)

    summary = {
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "candidate_run": str(run_path),
            "baseline_audio_path": str(Path(baseline_audio_path).expanduser().resolve()),
            "reference_audio_path": str(Path(reference_audio_path).expanduser().resolve()),
            "config_path": str(Path(config_path).expanduser().resolve()),
            "profile_path": str(Path(profile_path).expanduser().resolve()),
            "scene_spec_path": str(Path(scene_spec_path).expanduser().resolve()) if scene_spec_path else "",
            "demo_readiness_path": str(Path(demo_readiness_path).expanduser().resolve()) if demo_readiness_path else "",
        },
        "verdicts": {
            "bench_passed": bench_passed,
            "demo_ready": demo_ready,
            "overall_passed": overall_passed,
        },
        "artifacts": {
            "capture_bundle": str(written_capture["bundle_path"]),
            "scene_manifest": str(written_capture["manifest_path"]),
            "scene_timing_metadata": str(written_capture["scene_timing_path"]),
            "bench_report": str(bench_report["output_path"]),
            "panel_scorecard_json": str(written_panel["json_path"]),
            "panel_scorecard_markdown": str(written_panel["markdown_path"]),
            "plots_dir": str(written_panel["plots_dir"]),
            "summary_json": str((out_dir / "demo_benchmark_summary.json").resolve()),
            "summary_markdown": str((out_dir / "demo_benchmark_summary.md").resolve()),
        },
    }
    written_summary = write_demo_benchmark_summary(summary, out_dir)
    summary["artifacts"]["summary_json"] = str(written_summary["json_path"])
    summary["artifacts"]["summary_markdown"] = str(written_summary["markdown_path"])
    return summary


def write_demo_benchmark_summary(summary: Dict[str, Any], output_dir: str | Path) -> Dict[str, Path]:
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "demo_benchmark_summary.json"
    markdown_path = out_dir / "demo_benchmark_summary.md"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_summary_markdown(summary), encoding="utf-8")
    return {"json_path": json_path, "markdown_path": markdown_path}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run capture bundle, FocusBench, and panel packet in one command")
    parser.add_argument("--candidate-run", required=True, help="FocusField candidate run directory or artifacts/LATEST pointer")
    parser.add_argument("--baseline-audio", required=True, help="MacBook built-in microphone WAV path")
    parser.add_argument("--reference-audio", required=True, help="Close-talk reference WAV path")
    parser.add_argument("--output-dir", required=True, help="Output folder for the full benchmark packet")
    parser.add_argument("--config", default="configs/meeting_peripheral_demo_safe.yaml", help="Config used for threshold loading")
    parser.add_argument("--profile", default=str(default_pi_nightly_profile_path()), help="Shared benchmark profile YAML path")
    parser.add_argument("--scene-spec", default="", help="Optional scene timing YAML/JSON path")
    parser.add_argument("--video-path", action="append", default=[], help="Optional camera video path")
    parser.add_argument("--room-profile", default="noisy_office_room", help="Named room profile")
    parser.add_argument("--meeting-app", default="Zoom", help="Meeting app used in the capture")
    parser.add_argument("--baseline-device-name", default="MacBook Built-in Microphone")
    parser.add_argument("--reference-device-name", default="Close-talk Reference Mic")
    parser.add_argument("--candidate-device-name", default="FocusField USB Mic")
    parser.add_argument("--sync-marker-time-s", type=float, default=None, help="Optional sync clap/chirp time in seconds")
    parser.add_argument("--demo-readiness", default="", help="Optional demo_readiness.json path")
    parser.add_argument("--strict-truth", action="store_true", help="Require label-backed truth metrics")
    parser.add_argument("--require-pass", action="store_true", help="Exit non-zero if the benchmark verdict is not fully green")
    args = parser.parse_args(argv)

    summary = run_demo_benchmark_pipeline(
        candidate_run=args.candidate_run,
        baseline_audio_path=args.baseline_audio,
        reference_audio_path=args.reference_audio,
        output_dir=args.output_dir,
        config_path=args.config,
        profile_path=args.profile,
        scene_spec_path=args.scene_spec or None,
        video_paths=args.video_path,
        room_profile=args.room_profile,
        meeting_app=args.meeting_app,
        baseline_device_name=args.baseline_device_name,
        reference_device_name=args.reference_device_name,
        candidate_device_name=args.candidate_device_name,
        sync_marker_time_s=args.sync_marker_time_s,
        demo_readiness_path=args.demo_readiness or None,
        strict_truth=bool(args.strict_truth),
    )
    print(json.dumps(summary["artifacts"], indent=2, sort_keys=True))
    if bool(args.require_pass) and not bool(summary["verdicts"]["overall_passed"]):
        return 2
    return 0


def _bench_thresholds(config: Dict[str, Any]) -> Dict[str, float]:
    bench_cfg = config.get("bench", {})
    if not isinstance(bench_cfg, dict):
        bench_cfg = {}
    targets = bench_cfg.get("targets", {})
    if not isinstance(targets, dict):
        targets = {}
    out: Dict[str, float] = {}
    for key, value in targets.items():
        try:
            out[str(key)] = float(value)
        except Exception:
            continue
    return out


def _render_summary_markdown(summary: Dict[str, Any]) -> str:
    verdicts = summary.get("verdicts", {}) if isinstance(summary.get("verdicts"), dict) else {}
    artifacts = summary.get("artifacts", {}) if isinstance(summary.get("artifacts"), dict) else {}
    inputs = summary.get("inputs", {}) if isinstance(summary.get("inputs"), dict) else {}
    demo_ready = verdicts.get("demo_ready")
    demo_ready_text = "n/a" if demo_ready is None else ("PASS" if demo_ready else "FAIL")
    lines = [
        "# FocusField Demo Benchmark Summary",
        "",
        f"- Candidate run: {inputs.get('candidate_run', '')}",
        f"- Bench verdict: {'PASS' if verdicts.get('bench_passed') else 'FAIL'}",
        f"- Demo readiness: {demo_ready_text}",
        f"- Overall verdict: {'PASS' if verdicts.get('overall_passed') else 'FAIL'}",
        "",
        "## Artifacts",
        f"- Capture bundle: {artifacts.get('capture_bundle', '')}",
        f"- Scene manifest: {artifacts.get('scene_manifest', '')}",
        f"- Bench report: {artifacts.get('bench_report', '')}",
        f"- Panel scorecard: {artifacts.get('panel_scorecard_markdown', '')}",
        f"- Summary JSON: {artifacts.get('summary_json', '')}",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())

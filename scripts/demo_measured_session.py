#!/usr/bin/env python3
"""Helpers for measured live demo sessions.

This script avoids hand-stitching the operator path:
- dual local WAV capture for baseline/reference
- scene template generation for manual labels
- readiness + benchmark packet generation from one measured run
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from scripts.demo_ab_capture import build_demo_capture_bundle
from scripts.demo_benchmark_pipeline import run_demo_benchmark_pipeline
from scripts.demo_rehearsal_gate import build_demo_readiness, write_demo_readiness
from scripts.support_bundle import resolve_run_dir


DEFAULT_BASELINE_DEVICE = "MacBook Pro Microphone"
DEFAULT_REFERENCE_DEVICE = "Omar’s iPhone Microphone"
DEFAULT_DURATION_S = 180.0
DEFAULT_SAMPLE_RATE_HZ = 48000
DEFAULT_CHANNELS = 1
DEFAULT_SCENE_ID = "live_measured_session"
DEFAULT_MEETING_APP = "Zoom"
DEFAULT_ROOM_PROFILE = "noisy_office_room"


def list_avfoundation_audio_devices(*, ffmpeg_bin: str = "ffmpeg") -> List[Dict[str, Any]]:
    if shutil.which(ffmpeg_bin) is None:
        raise FileNotFoundError(f"ffmpeg not found on PATH: {ffmpeg_bin}")
    cmd = [ffmpeg_bin, "-f", "avfoundation", "-list_devices", "true", "-i", ""]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    text = f"{proc.stdout}\n{proc.stderr}"
    devices: List[Dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        marker = "AVFoundation indev @"
        if marker not in line or "audio devices:" in line.lower():
            continue
        if "] [" not in line:
            continue
        try:
            suffix = line.rsplit("] [", 1)[1]
            index_text, name = suffix.split("] ", 1)
            devices.append({"index": int(index_text), "name": name.strip()})
        except Exception:
            continue
    return devices


def build_host_audio_capture_plan(
    *,
    output_dir: str,
    baseline_device: str,
    reference_device: str,
    duration_s: float = DEFAULT_DURATION_S,
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
    channels: int = DEFAULT_CHANNELS,
    start_delay_s: float = 3.0,
    ffmpeg_bin: str = "ffmpeg",
) -> Dict[str, Any]:
    out_dir = Path(output_dir).expanduser().resolve()
    captures = []
    for role, device_name in (
        ("baseline", baseline_device),
        ("reference", reference_device),
    ):
        output_path = out_dir / f"{role}.wav"
        argv = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "avfoundation",
            "-i",
            f":{device_name}",
            "-t",
            f"{float(duration_s):.3f}",
            "-ac",
            str(int(channels)),
            "-ar",
            str(int(sample_rate_hz)),
            str(output_path),
        ]
        captures.append(
            {
                "role": role,
                "device_name": device_name,
                "output_path": str(output_path),
                "argv": argv,
                "command": shlex.join(argv),
            }
        )
    return {
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(out_dir),
        "duration_s": float(duration_s),
        "sample_rate_hz": int(sample_rate_hz),
        "channels": int(channels),
        "start_delay_s": float(start_delay_s),
        "captures": captures,
    }


def execute_host_audio_capture(plan: Dict[str, Any]) -> Dict[str, Any]:
    captures = plan.get("captures", [])
    if not isinstance(captures, list) or len(captures) < 2:
        raise ValueError("capture plan must include baseline and reference captures")
    ffmpeg_bin = str(captures[0].get("argv", ["ffmpeg"])[0])
    if shutil.which(ffmpeg_bin) is None:
        raise FileNotFoundError(f"ffmpeg not found on PATH: {ffmpeg_bin}")

    out_dir = Path(str(plan["output_dir"])).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    delay_s = float(plan.get("start_delay_s", 0.0) or 0.0)
    if delay_s > 0.0:
        time.sleep(delay_s)

    started_at = datetime.now(timezone.utc).isoformat()
    procs: List[tuple[subprocess.Popen[str], Dict[str, Any]]] = []
    for capture in captures:
        argv = [str(item) for item in capture.get("argv", [])]
        procs.append(
            (
                subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True),
                capture,
            )
        )

    results = []
    exit_code = 0
    for proc, capture in procs:
        stderr = proc.communicate()[1]
        code = int(proc.returncode or 0)
        if code != 0 and exit_code == 0:
            exit_code = code
        results.append(
            {
                "role": capture["role"],
                "device_name": capture["device_name"],
                "output_path": capture["output_path"],
                "returncode": code,
                "stderr": stderr.strip(),
            }
        )

    return {
        "schema_version": "1.0.0",
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(out_dir),
        "results": results,
        "returncode": exit_code,
    }


def write_measured_scene_template(
    *,
    candidate_run: str,
    baseline_audio_path: str,
    reference_audio_path: str,
    output_path: str,
    scene_id: str = DEFAULT_SCENE_ID,
    meeting_app: str = DEFAULT_MEETING_APP,
    room_profile: str = DEFAULT_ROOM_PROFILE,
) -> Path:
    payload = build_demo_capture_bundle(
        candidate_run=candidate_run,
        baseline_audio_path=baseline_audio_path,
        reference_audio_path=reference_audio_path,
        output_dir=str(Path(output_path).expanduser().resolve().parent),
        room_profile=room_profile,
        meeting_app=meeting_app,
        dry_run=True,
    )
    manifest = payload["manifest"]
    scenes = manifest.get("scenes", [])
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("expected at least one scene in capture manifest")
    scene = dict(scenes[0])
    scene["scene_id"] = str(scene_id)
    scene["description"] = "Measured live demo session. Fill in labels after the run."
    scene["tags"] = ["demo", "live", "measured", room_profile]
    scene["speaker_segments"] = []
    scene["bearing_segments"] = []
    scene["reference_text"] = ""
    scene["baseline_text"] = ""
    scene["candidate_text"] = ""
    manifest["scenes"] = [scene]

    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def run_measured_demo_packet(
    *,
    candidate_run: str,
    baseline_audio_path: str,
    reference_audio_path: str,
    output_dir: str,
    host_gate_evidence: Optional[str] = None,
    config_path: str = "configs/meeting_peripheral_demo_safe.yaml",
    scene_spec_path: Optional[str] = None,
    strict_truth: bool = False,
) -> Dict[str, Any]:
    run_dir = resolve_run_dir(candidate_run)
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    scene_template_path = (
        Path(scene_spec_path).expanduser().resolve()
        if scene_spec_path
        else write_measured_scene_template(
            candidate_run=str(run_dir),
            baseline_audio_path=baseline_audio_path,
            reference_audio_path=reference_audio_path,
            output_path=str(out_dir / "scene_labels_template.yaml"),
        )
    )

    demo_readiness_path = out_dir / "demo_readiness.json"
    readiness_payload = build_demo_readiness(
        config_path,
        run_dir=str(run_dir),
        host_gate_evidence=host_gate_evidence,
    )
    write_demo_readiness(readiness_payload, demo_readiness_path)

    summary = run_demo_benchmark_pipeline(
        candidate_run=str(run_dir),
        baseline_audio_path=baseline_audio_path,
        reference_audio_path=reference_audio_path,
        output_dir=str(out_dir / "full_packet"),
        config_path=config_path,
        scene_spec_path=str(scene_template_path),
        demo_readiness_path=str(demo_readiness_path),
        strict_truth=bool(strict_truth),
    )
    summary["scene_template_path"] = str(scene_template_path)
    summary["demo_readiness_path"] = str(demo_readiness_path)
    return summary


def _main_list_audio_devices(args: argparse.Namespace) -> int:
    devices = list_avfoundation_audio_devices(ffmpeg_bin=args.ffmpeg_bin)
    print(json.dumps({"audio_devices": devices}, indent=2))
    return 0


def _main_record_host_audio(args: argparse.Namespace) -> int:
    plan = build_host_audio_capture_plan(
        output_dir=args.output_dir,
        baseline_device=args.baseline_device,
        reference_device=args.reference_device,
        duration_s=args.duration_s,
        sample_rate_hz=args.sample_rate_hz,
        channels=args.channels,
        start_delay_s=args.start_delay_s,
        ffmpeg_bin=args.ffmpeg_bin,
    )
    plan_path = Path(args.output_dir).expanduser().resolve() / "host_audio_capture_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    if args.print_only:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    result = execute_host_audio_capture(plan)
    result_path = Path(args.output_dir).expanduser().resolve() / "host_audio_capture_result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return int(result.get("returncode", 0) or 0)


def _main_write_scene_template(args: argparse.Namespace) -> int:
    path = write_measured_scene_template(
        candidate_run=args.candidate_run,
        baseline_audio_path=args.baseline_audio,
        reference_audio_path=args.reference_audio,
        output_path=args.output,
        scene_id=args.scene_id,
        meeting_app=args.meeting_app,
        room_profile=args.room_profile,
    )
    print(path)
    return 0


def _main_build_packet(args: argparse.Namespace) -> int:
    summary = run_measured_demo_packet(
        candidate_run=args.candidate_run,
        baseline_audio_path=args.baseline_audio,
        reference_audio_path=args.reference_audio,
        output_dir=args.output_dir,
        host_gate_evidence=args.host_gate_evidence or None,
        config_path=args.config,
        scene_spec_path=args.scene_spec or None,
        strict_truth=bool(args.strict_truth),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Measured live demo helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-audio-devices", help="List AVFoundation audio devices via ffmpeg")
    list_parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    list_parser.set_defaults(func=_main_list_audio_devices)

    record_parser = subparsers.add_parser("record-host-audio", help="Record baseline/reference WAVs concurrently")
    record_parser.add_argument("--output-dir", required=True)
    record_parser.add_argument("--baseline-device", default=DEFAULT_BASELINE_DEVICE)
    record_parser.add_argument("--reference-device", default=DEFAULT_REFERENCE_DEVICE)
    record_parser.add_argument("--duration-s", type=float, default=DEFAULT_DURATION_S)
    record_parser.add_argument("--sample-rate-hz", type=int, default=DEFAULT_SAMPLE_RATE_HZ)
    record_parser.add_argument("--channels", type=int, default=DEFAULT_CHANNELS)
    record_parser.add_argument("--start-delay-s", type=float, default=3.0)
    record_parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    record_parser.add_argument("--print-only", action="store_true")
    record_parser.set_defaults(func=_main_record_host_audio)

    scene_parser = subparsers.add_parser("write-scene-template", help="Write a label-ready scene YAML template")
    scene_parser.add_argument("--candidate-run", required=True)
    scene_parser.add_argument("--baseline-audio", required=True)
    scene_parser.add_argument("--reference-audio", required=True)
    scene_parser.add_argument("--output", required=True)
    scene_parser.add_argument("--scene-id", default=DEFAULT_SCENE_ID)
    scene_parser.add_argument("--meeting-app", default=DEFAULT_MEETING_APP)
    scene_parser.add_argument("--room-profile", default=DEFAULT_ROOM_PROFILE)
    scene_parser.set_defaults(func=_main_write_scene_template)

    packet_parser = subparsers.add_parser("build-packet", help="Generate readiness and one-shot benchmark packet")
    packet_parser.add_argument("--candidate-run", required=True)
    packet_parser.add_argument("--baseline-audio", required=True)
    packet_parser.add_argument("--reference-audio", required=True)
    packet_parser.add_argument("--output-dir", required=True)
    packet_parser.add_argument("--host-gate-evidence", default="")
    packet_parser.add_argument("--config", default="configs/meeting_peripheral_demo_safe.yaml")
    packet_parser.add_argument("--scene-spec", default="")
    packet_parser.add_argument("--strict-truth", action="store_true")
    packet_parser.set_defaults(func=_main_build_packet)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

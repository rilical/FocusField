#!/usr/bin/env python3
"""Assemble a same-session demo A/B benchmark bundle."""

from __future__ import annotations

import argparse
import json
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from scripts.support_bundle import resolve_run_dir


DEFAULT_ROOM_PROFILE = "noisy_office_room"
DEFAULT_MEETING_APP = "Zoom"
DEFAULT_REQUIRED_METRICS = [
    "si_sdr_delta_db",
    "stoi_delta",
    "wer_relative_improvement",
    "sir_delta_db",
    "latency_p95_ms",
    "output_underrun_rate",
    "queue_pressure",
]


def build_demo_capture_bundle(
    *,
    candidate_run: str,
    baseline_audio_path: str,
    reference_audio_path: str,
    output_dir: str,
    scene_spec_path: Optional[str] = None,
    video_paths: Optional[Sequence[str]] = None,
    room_profile: str = DEFAULT_ROOM_PROFILE,
    meeting_app: str = DEFAULT_MEETING_APP,
    baseline_device_name: str = "MacBook Built-in Microphone",
    reference_device_name: str = "Close-talk Reference Mic",
    candidate_device_name: str = "FocusField USB Mic",
    sync_marker_time_s: Optional[float] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    run_path = resolve_run_dir(candidate_run)
    output_path = Path(output_dir).expanduser().resolve()
    baseline_path = Path(baseline_audio_path).expanduser().resolve()
    reference_path = Path(reference_audio_path).expanduser().resolve()
    resolved_video_paths = [str(Path(path).expanduser().resolve()) for path in (video_paths or [])]

    candidate_audio_path = (run_path / "audio" / "enhanced.wav").resolve()
    required_candidate = {
        "candidate_audio": candidate_audio_path,
        "raw_audio": (run_path / "audio" / "raw.wav").resolve(),
        "lock_trace": (run_path / "traces" / "lock.jsonl").resolve(),
        "faces_trace": (run_path / "traces" / "faces.jsonl").resolve(),
        "doa_trace": (run_path / "traces" / "doa.jsonl").resolve(),
        "perf_log": (run_path / "logs" / "perf.jsonl").resolve(),
        "events_log": (run_path / "logs" / "events.jsonl").resolve(),
    }
    missing_artifacts = _missing_paths(
        [baseline_path, reference_path, *[Path(path) for path in resolved_video_paths], *required_candidate.values()]
    )
    if missing_artifacts and not dry_run:
        raise FileNotFoundError("Missing benchmark artifacts: " + ", ".join(missing_artifacts))

    session_duration_s = _session_duration_s(candidate_audio_path, baseline_path, reference_path, dry_run=dry_run)
    scenes = _normalize_scene_specs(scene_spec_path, session_duration_s, room_profile=room_profile)

    bundle = {
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(dry_run),
        "meeting_app": meeting_app,
        "room_profile": room_profile,
        "candidate_run": str(run_path),
        "devices": {
            "candidate": candidate_device_name,
            "baseline": baseline_device_name,
            "reference": reference_device_name,
        },
        "sync_marker": {
            "time_s": float(sync_marker_time_s) if sync_marker_time_s is not None else None,
            "description": "clap",
        },
        "artifacts": {
            "candidate_audio_path": str(candidate_audio_path),
            "baseline_audio_path": str(baseline_path),
            "reference_audio_path": str(reference_path),
            "video_paths": resolved_video_paths,
            **{name: str(path) for name, path in required_candidate.items()},
        },
        "scene_timing_metadata": scenes,
        "missing_artifacts": missing_artifacts,
        "output_dir": str(output_path),
    }
    manifest = _build_scene_manifest(
        scenes=scenes,
        candidate_audio_path=str(candidate_audio_path),
        baseline_audio_path=str(baseline_path),
        reference_audio_path=str(reference_path),
        video_paths=resolved_video_paths,
        room_profile=room_profile,
        dry_run=dry_run,
    )
    return {
        "bundle": bundle,
        "manifest": manifest,
        "output_dir": str(output_path),
    }


def write_demo_capture_bundle(payload: Dict[str, Any], output_dir: str | Path) -> Dict[str, Path]:
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = out_dir / "capture_bundle.json"
    manifest_path = out_dir / "scene_manifest.yaml"
    scene_path = out_dir / "scene_timing_metadata.json"
    bundle_path.write_text(json.dumps(payload["bundle"], indent=2, sort_keys=True), encoding="utf-8")
    manifest_path.write_text(yaml.safe_dump(payload["manifest"], sort_keys=False), encoding="utf-8")
    scene_path.write_text(
        json.dumps(payload["bundle"].get("scene_timing_metadata", []), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "bundle_path": bundle_path,
        "manifest_path": manifest_path,
        "scene_timing_path": scene_path,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble a same-session demo A/B bundle")
    parser.add_argument("--candidate-run", required=True, help="FocusField candidate run directory")
    parser.add_argument("--baseline-audio", required=True, help="MacBook built-in mic WAV path")
    parser.add_argument("--reference-audio", required=True, help="Close-talk reference WAV path")
    parser.add_argument("--output-dir", required=True, help="Output folder for bundle metadata")
    parser.add_argument("--scene-spec", default="", help="Optional YAML/JSON scene timing file")
    parser.add_argument("--video-path", action="append", default=[], help="Optional recorded camera video path")
    parser.add_argument("--room-profile", default=DEFAULT_ROOM_PROFILE, help="Named demo room profile")
    parser.add_argument("--meeting-app", default=DEFAULT_MEETING_APP, help="Meeting app used in the demo")
    parser.add_argument("--baseline-device-name", default="MacBook Built-in Microphone")
    parser.add_argument("--reference-device-name", default="Close-talk Reference Mic")
    parser.add_argument("--candidate-device-name", default="FocusField USB Mic")
    parser.add_argument("--sync-marker-time-s", type=float, default=None, help="Sync clap/chirp time in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Write bundle metadata without strict artifact validation")
    args = parser.parse_args(argv)

    payload = build_demo_capture_bundle(
        candidate_run=args.candidate_run,
        baseline_audio_path=args.baseline_audio,
        reference_audio_path=args.reference_audio,
        output_dir=args.output_dir,
        scene_spec_path=args.scene_spec or None,
        video_paths=args.video_path,
        room_profile=args.room_profile,
        meeting_app=args.meeting_app,
        baseline_device_name=args.baseline_device_name,
        reference_device_name=args.reference_device_name,
        candidate_device_name=args.candidate_device_name,
        sync_marker_time_s=args.sync_marker_time_s,
        dry_run=bool(args.dry_run),
    )
    written = write_demo_capture_bundle(payload, args.output_dir)
    print(json.dumps({key: str(value) for key, value in written.items()}, indent=2, sort_keys=True))
    return 0


def _normalize_scene_specs(scene_spec_path: Optional[str], session_duration_s: float, *, room_profile: str) -> List[Dict[str, Any]]:
    if not scene_spec_path:
        return [
            {
                "scene_id": "full_session",
                "description": "Full same-session A/B capture",
                "start_s": 0.0,
                "end_s": max(0.0, session_duration_s),
                "tags": ["demo", "ab", room_profile],
                "speaker_segments": [],
                "bearing_segments": [],
            }
        ]

    path = Path(scene_spec_path).expanduser().resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if isinstance(raw, dict):
        raw_scenes = raw.get("scenes", [])
    elif isinstance(raw, list):
        raw_scenes = raw
    else:
        raw_scenes = []

    scenes: List[Dict[str, Any]] = []
    for index, scene in enumerate(raw_scenes):
        if not isinstance(scene, dict):
            continue
        scene_id = str(scene.get("scene_id", f"scene_{index + 1}") or f"scene_{index + 1}").strip()
        start_s = _nonnegative_float(scene.get("start_s", scene.get("start", 0.0))) or 0.0
        end_s = _nonnegative_float(scene.get("end_s", scene.get("end", session_duration_s)))
        if end_s is None or end_s <= start_s:
            end_s = session_duration_s
        tags = _string_list(scene.get("tags")) or ["demo", "ab", room_profile]
        normalized = {
            "scene_id": scene_id,
            "description": str(scene.get("description", "") or ""),
            "start_s": start_s,
            "end_s": end_s,
            "tags": tags,
            "speaker_segments": scene.get("speaker_segments", []),
            "bearing_segments": scene.get("bearing_segments", []),
        }
        for key in ("labels", "reference_text", "baseline_text", "candidate_text", "noise_reference_audio_path"):
            if key in scene:
                normalized[key] = scene[key]
        scenes.append(normalized)
    return scenes


def _build_scene_manifest(
    *,
    scenes: Sequence[Dict[str, Any]],
    candidate_audio_path: str,
    baseline_audio_path: str,
    reference_audio_path: str,
    video_paths: Sequence[str],
    room_profile: str,
    dry_run: bool,
) -> Dict[str, Any]:
    manifest_scenes: List[Dict[str, Any]] = []
    for scene in scenes:
        tags = _string_list(scene.get("tags")) or ["demo", "ab", room_profile]
        manifest_scene: Dict[str, Any] = {
            "scene_id": str(scene.get("scene_id", "") or "").strip(),
            "description": str(scene.get("description", "") or ""),
            "start_s": float(scene.get("start_s", 0.0) or 0.0),
            "end_s": float(scene.get("end_s", 0.0) or 0.0),
            "audio_path": str(candidate_audio_path),
            "reference_audio_path": str(reference_audio_path),
            "baseline_audio_path": str(baseline_audio_path),
            "candidate_audio_path": str(candidate_audio_path),
            "video_paths": list(video_paths),
            "speaker_segments": scene.get("speaker_segments", []),
            "bearing_segments": scene.get("bearing_segments", []),
            "required_metrics": list(DEFAULT_REQUIRED_METRICS),
            "tags": tags,
        }
        for key in ("labels", "reference_text", "baseline_text", "candidate_text"):
            if key in scene:
                manifest_scene[key] = scene[key]
        noise_ref = scene.get("noise_reference_audio_path")
        if noise_ref:
            manifest_scene["noise_reference_audio_path"] = str(Path(str(noise_ref)).expanduser().resolve())
        manifest_scenes.append(manifest_scene)
    return {
        "version": 1,
        "room_profile": room_profile,
        "dry_run": bool(dry_run),
        "scenes": manifest_scenes,
    }


def _session_duration_s(candidate_audio_path: Path, baseline_path: Path, reference_path: Path, *, dry_run: bool) -> float:
    if dry_run:
        return max(1.0, _wav_duration_s(candidate_audio_path) or _wav_duration_s(baseline_path) or _wav_duration_s(reference_path) or 30.0)
    durations = [value for value in (_wav_duration_s(candidate_audio_path), _wav_duration_s(baseline_path), _wav_duration_s(reference_path)) if value is not None]
    if not durations:
        raise ValueError("Unable to determine benchmark session duration from candidate/baseline/reference WAV files")
    return float(min(durations))


def _wav_duration_s(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    try:
        with wave.open(str(path), "rb") as handle:
            frames = int(handle.getnframes())
            sample_rate_hz = int(handle.getframerate())
    except Exception:
        return None
    if sample_rate_hz <= 0:
        return None
    return float(frames) / float(sample_rate_hz)


def _missing_paths(paths: Iterable[Path]) -> List[str]:
    missing: List[str] = []
    for path in paths:
        if not path.exists():
            missing.append(str(path))
    return missing


def _nonnegative_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out < 0.0:
        return None
    return out


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


if __name__ == "__main__":
    raise SystemExit(main())

"""FocusBench CLI and report generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import yaml

from focusfield.bench.metrics.metrics import (
    compute_drop_stats,
    compute_latency_stats,
    compute_lock_jitter,
    compute_scene_metric,
    scene_metric_to_dict,
    summarize_scene_metrics,
)
from focusfield.bench.metrics.scoring import evaluate_gates, normalize_thresholds
from focusfield.bench.reports.plots import generate_required_plots
from focusfield.bench.reports.report_schema import create_report, write_report


def run_focusbench(
    baseline_run: str,
    candidate_run: str,
    scene_manifest: str,
    output_dir: str,
    thresholds: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    scenes = _load_scenes(scene_manifest)
    if not scenes:
        raise ValueError("Scene manifest contains no scenes")

    scene_metrics = []
    for scene in scenes:
        scene_id = str(scene.get("scene_id", "scene"))
        baseline_audio = _resolve_audio_path(scene, baseline_run, role="baseline")
        candidate_audio = _resolve_audio_path(scene, candidate_run, role="candidate")
        if not baseline_audio.exists():
            raise FileNotFoundError(f"Baseline audio missing for scene={scene_id}: {baseline_audio}")
        if not candidate_audio.exists():
            raise FileNotFoundError(f"Candidate audio missing for scene={scene_id}: {candidate_audio}")
        metric = compute_scene_metric(scene, baseline_audio, candidate_audio)
        scene_metrics.append(metric)

    quality_summary = summarize_scene_metrics(scene_metrics)
    latency_summary = compute_latency_stats(Path(candidate_run) / "logs" / "perf.jsonl")
    drop_summary = compute_drop_stats(
        Path(candidate_run) / "logs" / "events.jsonl",
        Path(candidate_run) / "logs" / "perf.jsonl",
    )
    lock_summary = compute_lock_jitter(Path(candidate_run) / "traces" / "lock.jsonl")
    gates = evaluate_gates(
        quality_summary=quality_summary,
        latency_summary=latency_summary,
        drop_summary=drop_summary,
        thresholds=normalize_thresholds(thresholds),
    )

    report = create_report(
        baseline_run=baseline_run,
        candidate_run=candidate_run,
        scene_manifest=scene_manifest,
        scene_metrics=[scene_metric_to_dict(item) for item in scene_metrics],
        quality_summary=quality_summary,
        latency_summary=latency_summary,
        drop_summary=drop_summary,
        lock_jitter=lock_summary,
        gates=gates,
    )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    plots = generate_required_plots(report, output_path / "plots")
    report["plots"] = plots
    report_file = write_report(report, output_path / "BenchReport.json")
    report["output_path"] = str(report_file)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="FocusBench A/B report generator")
    parser.add_argument("--baseline-run", required=True, help="Baseline run directory (miniDSP DSP mode artifacts).")
    parser.add_argument("--candidate-run", required=True, help="Candidate run directory (FocusField RAW mode artifacts).")
    parser.add_argument("--scene-manifest", required=True, help="Scene manifest YAML path.")
    parser.add_argument("--output-dir", required=True, help="Output directory for BenchReport and plots.")
    parser.add_argument("--thresholds-json", default="", help="Optional JSON object to override gate thresholds.")
    args = parser.parse_args()

    threshold_overrides = _parse_threshold_overrides(args.thresholds_json)
    report = run_focusbench(
        baseline_run=args.baseline_run,
        candidate_run=args.candidate_run,
        scene_manifest=args.scene_manifest,
        output_dir=args.output_dir,
        thresholds=threshold_overrides,
    )

    gates = report.get("summary", {}).get("gates", {})
    passed = bool(gates.get("passed", False)) if isinstance(gates, dict) else False
    print(f"BenchReport: {report.get('output_path')}")
    print(f"Gate verdict: {'PASS' if passed else 'FAIL'}")
    if isinstance(gates, dict):
        for check in gates.get("checks", []):
            if not isinstance(check, dict):
                continue
            name = str(check.get("name", "check"))
            status = "PASS" if bool(check.get("passed", False)) else "FAIL"
            print(f"[{status}] {name}: actual={check.get('actual')} expected={check.get('expected')}")
    return 0 if passed else 2


def _parse_threshold_overrides(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except Exception as exc:
        raise ValueError(f"Invalid thresholds JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("thresholds-json must be a JSON object")
    return value


def _load_scenes(path: str | Path) -> List[Dict[str, Any]]:
    manifest_path = Path(path)
    with manifest_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if isinstance(data, dict):
        scenes = data.get("scenes", [])
        if isinstance(scenes, list):
            return [item for item in scenes if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _resolve_audio_path(scene: Dict[str, Any], run_dir: str, role: str) -> Path:
    override_key = f"{role}_audio_path"
    if override_key in scene:
        return Path(str(scene[override_key])).expanduser().resolve()
    default_name = "enhanced.wav"
    if role == "baseline":
        default_name = str(scene.get("baseline_audio_file", default_name))
    else:
        default_name = str(scene.get("candidate_audio_file", default_name))
    return (Path(run_dir) / "audio" / default_name).resolve()


if __name__ == "__main__":
    raise SystemExit(main())

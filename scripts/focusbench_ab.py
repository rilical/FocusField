#!/usr/bin/env python3
"""Run FocusBench A/B comparison with config-based gate thresholds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from focusfield.bench.focusbench import run_focusbench
from focusfield.core.config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description="FocusBench A/B gate checker")
    parser.add_argument("--baseline-run", required=True, help="Baseline artifacts run dir (DSP mode).")
    parser.add_argument("--candidate-run", required=True, help="Candidate artifacts run dir (FocusField RAW mode).")
    parser.add_argument("--scene-manifest", required=True, help="Scene manifest YAML file.")
    parser.add_argument("--output-dir", required=True, help="Output folder for BenchReport.")
    parser.add_argument(
        "--config",
        default="configs/full_3cam_8mic_pi.yaml",
        help="Config path used to resolve bench.targets thresholds.",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    thresholds = _bench_thresholds(cfg)
    report = run_focusbench(
        baseline_run=args.baseline_run,
        candidate_run=args.candidate_run,
        scene_manifest=args.scene_manifest,
        output_dir=args.output_dir,
        thresholds=thresholds,
    )
    gates = report.get("summary", {}).get("gates", {})
    passed = bool(gates.get("passed", False)) if isinstance(gates, dict) else False
    print(f"report={report.get('output_path')}")
    print(f"verdict={'PASS' if passed else 'FAIL'}")
    if isinstance(gates, dict):
        print(json.dumps(gates, indent=2, sort_keys=True))
    return 0 if passed else 2


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


if __name__ == "__main__":
    raise SystemExit(main())

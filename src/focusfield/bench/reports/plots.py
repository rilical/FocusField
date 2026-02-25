"""Required plot generation for FocusBench reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np


def generate_required_plots(report: Dict[str, Any], output_dir: str | Path) -> Dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return _write_plot_data_only(report, out)

    scene_rows = report.get("scene_metrics", [])
    if not isinstance(scene_rows, list):
        scene_rows = []
    latency = report.get("summary", {}).get("latency", {})
    if not isinstance(latency, dict):
        latency = {}

    outputs: Dict[str, str] = {}

    # 1) Steering proxy: SI-SDR delta vs target angle.
    steer_points = _pairs(scene_rows, "target_angle_deg", "si_sdr_delta_db")
    fig = plt.figure(figsize=(8, 4))
    ax = fig.add_subplot(1, 1, 1)
    if steer_points:
        xs, ys = zip(*steer_points)
        ax.scatter(xs, ys, s=28)
        ax.plot(xs, ys, alpha=0.35)
    ax.set_title("Steering Proxy: SI-SDR Delta vs Target Angle")
    ax.set_xlabel("Target Angle (deg)")
    ax.set_ylabel("SI-SDR Delta (dB)")
    ax.grid(True, alpha=0.3)
    path1 = out / "steering_proxy_vs_angle.png"
    fig.tight_layout()
    fig.savefig(path1, dpi=150)
    plt.close(fig)
    outputs["steering_proxy_vs_angle"] = str(path1)

    # 2) SIR improvement vs interferer angle.
    sir_points = _pairs(scene_rows, "interferer_angle_deg", "sir_delta_db")
    fig = plt.figure(figsize=(8, 4))
    ax = fig.add_subplot(1, 1, 1)
    if sir_points:
        xs, ys = zip(*sir_points)
        ax.scatter(xs, ys, s=28)
        ax.plot(xs, ys, alpha=0.35)
    ax.set_title("SIR Improvement vs Interferer Angle")
    ax.set_xlabel("Interferer Angle (deg)")
    ax.set_ylabel("SIR Delta (dB)")
    ax.grid(True, alpha=0.3)
    path2 = out / "sir_delta_vs_interferer_angle.png"
    fig.tight_layout()
    fig.savefig(path2, dpi=150)
    plt.close(fig)
    outputs["sir_delta_vs_interferer_angle"] = str(path2)

    # 3) Latency histogram from summary quantiles approximation.
    p50 = _opt_float(latency.get("p50_ms"))
    p95 = _opt_float(latency.get("p95_ms"))
    p99 = _opt_float(latency.get("p99_ms"))
    fig = plt.figure(figsize=(8, 4))
    ax = fig.add_subplot(1, 1, 1)
    bars_x = ["P50", "P95", "P99"]
    bars_y = [p50 or 0.0, p95 or 0.0, p99 or 0.0]
    ax.bar(bars_x, bars_y)
    ax.set_title("Latency Quantiles (ms)")
    ax.set_ylabel("ms")
    ax.grid(True, axis="y", alpha=0.3)
    path3 = out / "latency_quantiles.png"
    fig.tight_layout()
    fig.savefig(path3, dpi=150)
    plt.close(fig)
    outputs["latency_quantiles"] = str(path3)

    # 4) Target lock jitter summary.
    lock = report.get("summary", {}).get("lock_jitter", {})
    jitter = _opt_float(lock.get("mae_step_deg") if isinstance(lock, dict) else None) or 0.0
    stdv = _opt_float(lock.get("std_step_deg") if isinstance(lock, dict) else None) or 0.0
    fig = plt.figure(figsize=(8, 4))
    ax = fig.add_subplot(1, 1, 1)
    ax.bar(["MAE step", "STD step"], [jitter, stdv])
    ax.set_title("Target Lock Bearing Jitter")
    ax.set_ylabel("Degrees")
    ax.grid(True, axis="y", alpha=0.3)
    path4 = out / "target_lock_jitter.png"
    fig.tight_layout()
    fig.savefig(path4, dpi=150)
    plt.close(fig)
    outputs["target_lock_jitter"] = str(path4)

    return outputs


def _write_plot_data_only(report: Dict[str, Any], out: Path) -> Dict[str, str]:
    payload = {
        "note": "matplotlib_unavailable",
        "scene_metrics": report.get("scene_metrics", []),
        "summary": report.get("summary", {}),
    }
    path = out / "plot_data.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return {"plot_data": str(path)}


def _pairs(rows: List[Dict[str, Any]], x_key: str, y_key: str) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        x = _opt_float(row.get(x_key))
        y = _opt_float(row.get(y_key))
        if x is None or y is None:
            continue
        out.append((x, y))
    out.sort(key=lambda item: item[0])
    return out


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if np.isnan(float(value)):
            return None
        return float(value)
    except Exception:
        return None

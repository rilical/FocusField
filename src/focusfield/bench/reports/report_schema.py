"""Bench report schema helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


SCHEMA_VERSION = "1.0.0"


def create_report(
    baseline_run: str,
    candidate_run: str,
    scene_manifest: str,
    scene_metrics: list[dict[str, Any]],
    quality_summary: dict[str, Any],
    latency_summary: dict[str, Any],
    drop_summary: dict[str, Any],
    lock_jitter: dict[str, Any],
    conversation: dict[str, Any],
    gates: dict[str, Any],
    plots: Optional[dict[str, str]] = None,
) -> Dict[str, Any]:
    manifest_hash = _sha256_file(scene_manifest)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "baseline_run": str(baseline_run),
            "candidate_run": str(candidate_run),
            "scene_manifest": str(scene_manifest),
            "scene_manifest_sha256": manifest_hash,
        },
        "summary": {
            "quality": quality_summary,
            "latency": latency_summary,
            "drops": drop_summary,
            "lock_jitter": lock_jitter,
            "conversation": conversation,
            "gates": gates,
        },
        "scene_metrics": scene_metrics,
        "plots": plots or {},
    }


def write_report(report: Dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    return path


def _sha256_file(path: str | Path) -> Optional[str]:
    p = Path(path)
    if not p.exists():
        return None
    h = hashlib.sha256()
    with p.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

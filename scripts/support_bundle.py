#!/usr/bin/env python3
"""Generate a portable FocusField support bundle."""

from __future__ import annotations

import argparse
import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from focusfield.core.config import load_config
from focusfield.vision.calibration.runtime_overlay import get_camera_calibration_path


def resolve_run_dir(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if path.is_file():
        ref = path.read_text(encoding="utf-8").strip()
        if ref:
            return (path.parent / ref).resolve()
    return path.resolve()


def build_bundle_summary(
    run_dir: str | Path,
    config_path: str | None = None,
    service_name: str = "focusfield",
    max_tail_lines: int = 200,
) -> Dict[str, Any]:
    run_path = resolve_run_dir(str(run_dir))
    config_file = Path(config_path).expanduser().resolve() if config_path else None
    config: Dict[str, Any] = {}
    if config_file is not None and config_file.exists():
        try:
            config = load_config(str(config_file))
        except Exception:
            config = {}
    base_dir = config_file.parent if config_file is not None else Path.cwd()
    calibration_path = get_camera_calibration_path(base_dir=base_dir)

    return {
        "run_dir": str(run_path),
        "config_path": str(config_file) if config_file is not None else "",
        "service_name": service_name,
        "files": _known_files(run_path, calibration_path),
        "run_meta": _read_json_file(run_path / "run_meta.json"),
        "service": _service_summary(service_name, max_tail_lines=max_tail_lines),
        "latest_perf": _read_last_jsonl_entry(run_path / "logs" / "perf.jsonl"),
        "latest_event": _read_last_jsonl_entry(run_path / "logs" / "events.jsonl"),
        "gate_outputs": _gate_outputs(run_path),
        "calibration_path": str(calibration_path),
        "calibration_exists": calibration_path.exists(),
    }


def create_support_bundle(
    run_dir: str | Path,
    output_path: str | Path,
    config_path: str | None = None,
    service_name: str = "focusfield",
    max_tail_lines: int = 200,
) -> Path:
    run_path = resolve_run_dir(str(run_dir))
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = build_bundle_summary(run_path, config_path=config_path, service_name=service_name, max_tail_lines=max_tail_lines)
    calibration_path = Path(summary["calibration_path"])

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.json", json.dumps(summary, indent=2, sort_keys=True))
        for rel_path in (
            Path("run_meta.json"),
            Path("config_effective.yaml"),
            Path("logs/perf.jsonl"),
            Path("logs/events.jsonl"),
        ):
            src = run_path / rel_path
            if src.exists():
                zf.write(src, arcname=str(Path("run") / rel_path))
        if calibration_path.exists():
            zf.write(calibration_path, arcname="camera_calibration.json")
    return output


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a FocusField support bundle")
    parser.add_argument("--run-dir", default="artifacts/LATEST", help="Run directory or artifacts/LATEST pointer")
    parser.add_argument("--output", required=True, help="Zip file to write")
    parser.add_argument("--config", default="", help="Optional config path used to locate calibration")
    parser.add_argument("--service-name", default="focusfield", help="System service name")
    parser.add_argument("--max-tail-lines", type=int, default=200, help="Max service log lines to include in summary")
    args = parser.parse_args(argv)

    bundle = create_support_bundle(
        run_dir=args.run_dir,
        output_path=args.output,
        config_path=args.config or None,
        service_name=args.service_name,
        max_tail_lines=max(10, int(args.max_tail_lines)),
    )
    print(bundle)
    return 0


def _gate_outputs(run_path: Path) -> Dict[str, Any]:
    outputs: Dict[str, Any] = {}
    for pattern in ("gates/*.json", "reports/*.json", "BenchReport.json"):
        for item in run_path.glob(pattern):
            payload = _read_json_file(item)
            if payload is not None:
                outputs[str(item.relative_to(run_path))] = payload
    return outputs


def _known_files(run_path: Path, calibration_path: Path) -> Dict[str, bool]:
    return {
        "run_meta": (run_path / "run_meta.json").exists(),
        "config_effective": (run_path / "config_effective.yaml").exists(),
        "perf_log": (run_path / "logs" / "perf.jsonl").exists(),
        "event_log": (run_path / "logs" / "events.jsonl").exists(),
        "camera_calibration": calibration_path.exists(),
    }


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_last_jsonl_entry(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    last: Optional[Dict[str, Any]] = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    last = payload
    except Exception:
        return None
    return last


def _service_summary(service_name: str, max_tail_lines: int) -> Dict[str, Any]:
    return {
        "status": _command_output(["systemctl", "status", service_name, "--no-pager"]),
        "show": _command_output(["systemctl", "show", service_name, "--no-pager"]),
        "journal_tail": _command_output(
            ["journalctl", "-u", service_name, "-n", str(max_tail_lines), "--no-pager"]
        ),
    }


def _command_output(cmd: Iterable[str]) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            list(cmd),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return {"available": False, "returncode": None, "stdout": "", "stderr": "command not found"}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "returncode": None, "stdout": "", "stderr": str(exc)}
    return {
        "available": True,
        "returncode": int(completed.returncode),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


if __name__ == "__main__":
    raise SystemExit(main())

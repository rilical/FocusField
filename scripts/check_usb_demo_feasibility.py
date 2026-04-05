#!/usr/bin/env python3
"""Collect and evaluate direct USB-to-Mac demo feasibility evidence."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for candidate in (REPO_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from focusfield.audio.output.virtual_mic import list_output_devices


CommandRunner = Callable[[List[str]], Dict[str, Any]]


def collect_appliance_snapshot(
    *,
    model_path: str | Path = "/proc/device-tree/model",
    udc_root: str | Path = "/sys/class/udc",
    runner: CommandRunner | None = None,
) -> Dict[str, Any]:
    run = runner or _run_command
    model_file = Path(model_path)
    udc_dir = Path(udc_root)
    model = ""
    if model_file.exists():
        model = model_file.read_text(encoding="utf-8", errors="ignore").replace("\x00", "").strip()
    udc_names = sorted(item.name for item in udc_dir.iterdir()) if udc_dir.exists() else []
    lsusb_tree = run(["lsusb", "-t"])
    output_devices: List[Dict[str, Any]] = []
    output_devices_error = ""
    try:
        output_devices = [asdict(device) for device in list_output_devices()]
    except Exception as exc:  # pragma: no cover - backend failures are environment-specific
        output_devices_error = f"{type(exc).__name__}: {exc}"
    return {
        "platform": platform.system(),
        "model": model,
        "model_path": str(model_file),
        "udc_root": str(udc_dir),
        "udc_names": udc_names,
        "lsusb_tree": lsusb_tree,
        "output_devices": output_devices,
        "output_devices_error": output_devices_error,
    }


def evaluate_appliance_snapshot(
    snapshot: Dict[str, Any],
    *,
    connector_port: str,
    required_output_device: str = "",
) -> Dict[str, Any]:
    reasons: List[str] = []
    normalized_port = str(connector_port or "").strip().lower()
    if normalized_port in {"usb-a", "usb-a-host", "host-only"}:
        reasons.append("connector_port_host_only")
    udc_names = snapshot.get("udc_names") or []
    if not isinstance(udc_names, list) or not udc_names:
        reasons.append("no_udc_controller")
    output_devices_error = str(snapshot.get("output_devices_error", "") or "").strip()
    if output_devices_error:
        reasons.append("output_device_inventory_failed")

    available_output_names = [
        str(item.get("name", "")).strip()
        for item in snapshot.get("output_devices", [])
        if isinstance(item, dict)
    ]
    if required_output_device and required_output_device not in available_output_names:
        reasons.append("required_output_device_missing")

    return {
        "platform": snapshot.get("platform"),
        "connector_port": normalized_port,
        "required_output_device": required_output_device,
        "available_output_names": available_output_names,
        "output_devices_error": output_devices_error,
        "passed": not reasons,
        "reasons": reasons,
        "snapshot": snapshot,
    }


def collect_macos_snapshot(*, runner: CommandRunner | None = None) -> Dict[str, Any]:
    run = runner or _run_command
    usb = run(["system_profiler", "SPUSBDataType", "-json"])
    audio = run(["system_profiler", "SPAudioDataType", "-json"])
    return {
        "platform": platform.system(),
        "usb": usb,
        "audio": audio,
    }


def compare_macos_snapshots(
    before: Dict[str, Any],
    after: Dict[str, Any],
    *,
    expected_device_name: str,
) -> Dict[str, Any]:
    reasons: List[str] = []
    target = str(expected_device_name or "").strip()
    before_strings = _flatten_strings(before)
    after_strings = _flatten_strings(after)
    present_before = any(target in value for value in before_strings)
    present_after = any(target in value for value in after_strings)
    appeared_in_usb = any(target in value for value in _flatten_strings(after.get("usb", {})))
    appeared_in_audio = any(target in value for value in _flatten_strings(after.get("audio", {})))

    if not present_after:
        reasons.append("expected_device_missing_after_attach")
    if present_before:
        reasons.append("expected_device_already_present_before_attach")
    if present_after and not appeared_in_audio:
        reasons.append("expected_device_missing_from_audio_inventory")
    if present_after and not appeared_in_usb:
        reasons.append("expected_device_missing_from_usb_inventory")

    return {
        "platform": after.get("platform"),
        "expected_device_name": target,
        "present_before": present_before,
        "present_after": present_after,
        "appeared_in_usb": appeared_in_usb,
        "appeared_in_audio": appeared_in_audio,
        "passed": not reasons,
        "reasons": reasons,
        "before": before,
        "after": after,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect and evaluate direct USB demo feasibility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    appliance = subparsers.add_parser("appliance", help="Collect appliance-side USB demo readiness evidence")
    appliance.add_argument(
        "--connector-port",
        default="unknown",
        choices=("unknown", "usb-c-otg", "usb-a-host"),
        help="Physical port intended for the Mac connection",
    )
    appliance.add_argument(
        "--require-output-device",
        default="",
        help="Require a specific local output device name to be present",
    )
    appliance.add_argument("--output", default="", help="Optional JSON file to write")

    macos = subparsers.add_parser("macos-snapshot", help="Collect a macOS USB/audio inventory snapshot")
    macos.add_argument("--output", default="", help="Optional JSON file to write")

    compare = subparsers.add_parser("macos-compare", help="Compare macOS snapshots before/after cable attach")
    compare.add_argument("--before", required=True, help="Path to the 'before attach' JSON snapshot")
    compare.add_argument("--after", required=True, help="Path to the 'after attach' JSON snapshot")
    compare.add_argument("--expected-device-name", required=True, help="Expected host-visible device name")
    compare.add_argument("--output", default="", help="Optional JSON file to write")

    args = parser.parse_args(argv)
    if args.command == "appliance":
        snapshot = collect_appliance_snapshot()
        payload = evaluate_appliance_snapshot(
            snapshot,
            connector_port=args.connector_port,
            required_output_device=args.require_output_device,
        )
        _write_payload(payload, args.output)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["passed"] else 2

    if args.command == "macos-snapshot":
        payload = collect_macos_snapshot()
        _write_payload(payload, args.output)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    before = _load_json(args.before)
    after = _load_json(args.after)
    payload = compare_macos_snapshots(before, after, expected_device_name=args.expected_device_name)
    _write_payload(payload, args.output)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 2


def _write_payload(payload: Dict[str, Any], output_path: str) -> None:
    if not output_path:
        return
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _load_json(path_value: str | Path) -> Dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return payload


def _run_command(cmd: List[str]) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        return {"available": False, "returncode": None, "stdout": "", "stderr": str(exc), "command": cmd}
    return {
        "available": True,
        "returncode": int(completed.returncode),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": cmd,
    }


def _flatten_strings(value: Any) -> List[str]:
    out: List[str] = []
    _walk_strings(value, out)
    return out


def _walk_strings(value: Any, out: List[str]) -> None:
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _walk_strings(key, out)
            _walk_strings(item, out)
        return
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            _walk_strings(item, out)


if __name__ == "__main__":
    raise SystemExit(main())

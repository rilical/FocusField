#!/usr/bin/env python3
"""UMA-8 calibration helper.

This script is meant to be run on the target machine with the UMA-8 connected.

Step A: Channel-order "tap" test
  - You will be prompted to tap near each physical mic (clockwise).
  - The script records a short window and detects the channel with max RMS.
  - Output: suggested channel_order.

Step B: Orientation (yaw_offset_deg)
  - Place a speaker directly in front of cam0 (global 0°).
  - The script runs SRP-PHAT for a few seconds and reads the peak.
  - Output: suggested yaw_offset_deg.

The output is a YAML snippet you can paste into configs/device_profiles.yaml.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional, Tuple

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate UMA-8 channel order and yaw")
    parser.add_argument("--device", default=None, help="sounddevice device index or substring")
    parser.add_argument("--channels", type=int, default=8)
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--tap-seconds", type=float, default=0.4)
    parser.add_argument("--tap-count", type=int, default=7, help="number of ring mics to calibrate")
    parser.add_argument("--doa-seconds", type=float, default=4.0)
    parser.add_argument("--doa-bins", type=int, default=72)
    parser.add_argument("--ring-radius-m", type=float, default=0.042)
    args = parser.parse_args()

    if sd is None:
        raise SystemExit("sounddevice is required: pip install sounddevice")

    device_index = _resolve_device(args.device, args.channels)
    print(f"Using device_index={device_index}")

    print("\n=== Step A: tap test (channel order) ===")
    order = _tap_test(
        device_index=device_index,
        channels=args.channels,
        sample_rate=args.sample_rate,
        seconds=args.tap_seconds,
        tap_count=args.tap_count,
    )
    print(f"Suggested channel_order: {order}")

    print("\n=== Step B: orientation test (yaw_offset_deg) ===")
    print("Place a speaker directly in front of cam0 (global 0°), then press Enter.")
    input()
    yaw = _orientation_test(
        device_index=device_index,
        channel_order=order,
        ring_count=args.tap_count,
        channels=args.channels,
        sample_rate=args.sample_rate,
        seconds=args.doa_seconds,
        bins=args.doa_bins,
        ring_radius_m=args.ring_radius_m,
    )
    print(f"Suggested yaw_offset_deg: {yaw:.1f}")

    print("\n=== Paste into configs/device_profiles.yaml ===")
    print("mic_arrays:")
    print("  minidsp_uma8_raw_7p1:")
    print("    geometry: custom")
    print(f"    yaw_offset_deg: {yaw:.1f}")
    print("    positions_m:")
    for x, y in _uma8_positions(args.ring_radius_m):
        print(f"      - [{x:.6f}, {y:.6f}]")
    print(f"    channel_order: {order}")


def _resolve_device(device: Optional[str], channels: int) -> int:
    devices = sd.query_devices()
    if device is None:
        # Pick the first device with enough channels.
        for idx, d in enumerate(devices):
            if int(d.get("max_input_channels") or 0) >= channels:
                return idx
        return _default_input_index()

    text = str(device).strip()
    if not text:
        return int(sd.default.device[0])
    try:
        return int(text)
    except ValueError:
        pass
    lowered = text.lower()
    matches = []
    for idx, d in enumerate(devices):
        name = str(d.get("name") or "")
        if lowered in name.lower() and int(d.get("max_input_channels") or 0) >= channels:
            matches.append(idx)
    if matches:
        return matches[0]
    return _default_input_index()


def _tap_test(device_index: int, channels: int, sample_rate: int, seconds: float, tap_count: int) -> List[int]:
    observed: List[int] = []
    remaining = set(range(channels))
    for tap_idx in range(tap_count):
        print(f"Tap mic #{tap_idx} now (clockwise). Press Enter when ready.")
        input()
        data = sd.rec(int(seconds * sample_rate), samplerate=sample_rate, channels=channels, dtype="float32", device=device_index)
        sd.wait()
        x = np.asarray(data)
        rms = np.sqrt(np.mean(x**2, axis=0))
        # Prefer channels not already assigned.
        best = int(np.argmax(np.where(np.array([i in remaining for i in range(channels)]), rms, -1.0)))
        observed.append(best)
        remaining.discard(best)
        print(f"Detected channel {best} (rms={rms[best]:.6f})")
    # Append any remaining channels (e.g. center) in numeric order.
    observed.extend(sorted(remaining))
    return observed


def _default_input_index() -> int:
    default_idx = sd.default.device[0]
    if default_idx is None:
        raise RuntimeError("No default input device available. Plug in the UMA array and set it as default.")
    default_idx_int = int(default_idx)
    if default_idx_int < 0:
        raise RuntimeError("No valid default input device available. Select an input device in sounddevice settings or pass --device.")
    return default_idx_int


def _orientation_test(
    device_index: int,
    channel_order: List[int],
    ring_count: int,
    channels: int,
    sample_rate: int,
    seconds: float,
    bins: int,
    ring_radius_m: float,
) -> float:
    # Local SRP-PHAT: enough for yaw alignment.
    block = 1024
    total_samples = int(seconds * sample_rate)
    data = sd.rec(total_samples, samplerate=sample_rate, channels=channels, dtype="float32", device=device_index)
    sd.wait()
    x = np.asarray(data)
    ring_order = channel_order[: max(2, int(ring_count))]
    x = x[:, ring_order]

    angles_deg = np.linspace(0.0, 360.0, num=bins, endpoint=False)
    positions = _circular_positions(len(ring_order), ring_radius_m)
    heat = np.zeros((bins,), dtype=np.float32)
    # Process in blocks.
    for start in range(0, x.shape[0] - block, block):
        frame = x[start : start + block]
        heat += _srp_phat_frame(frame, sample_rate, angles_deg, positions)
    peak_angle = float(angles_deg[int(np.argmax(heat))])
    # yaw_offset should rotate array so that the peak becomes 0°.
    yaw_offset = (-peak_angle) % 360.0
    return yaw_offset


def _srp_phat_frame(frame: np.ndarray, sample_rate: int, angles_deg: np.ndarray, positions: np.ndarray) -> np.ndarray:
    n = frame.shape[0]
    spectrum = np.fft.rfft(frame, axis=0)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)

    # Build all pairs.
    ch = frame.shape[1]
    pairs = [(i, j) for i in range(ch) for j in range(i + 1, ch)]
    scores = np.zeros((angles_deg.shape[0],), dtype=np.float32)
    angles_rad = np.deg2rad(angles_deg)
    dir_vecs = np.stack([np.cos(angles_rad), np.sin(angles_rad)], axis=1)

    for (i, j) in pairs:
        cross = spectrum[:, i] * np.conj(spectrum[:, j])
        cross = cross / np.maximum(np.abs(cross), 1e-12)
        diff = positions[i] - positions[j]
        delays = (dir_vecs @ diff) / 343.0
        phase = np.exp(1j * 2.0 * np.pi * delays[:, None] * freqs[None, :])
        scores += np.real(phase @ cross).astype(np.float32)
    # Normalize.
    mx = float(scores.max()) if scores.size else 1.0
    if mx > 0:
        scores /= mx
    return scores


def _circular_positions(count: int, radius_m: float) -> np.ndarray:
    out = np.zeros((count, 2), dtype=np.float32)
    step = 2.0 * np.pi / float(count)
    for idx in range(count):
        ang = idx * step
        out[idx, 0] = float(radius_m * np.cos(ang))
        out[idx, 1] = float(radius_m * np.sin(ang))
    return out


def _uma8_positions(radius_m: float) -> List[Tuple[float, float]]:
    # 7 mic ring + center channel.
    ring = []
    for idx in range(7):
        ang = 2.0 * np.pi * idx / 7.0
        ring.append((float(radius_m * np.cos(ang)), float(radius_m * np.sin(ang))))
    ring.append((0.0, 0.0))
    return ring


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)

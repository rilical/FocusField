#!/usr/bin/env python3
"""Pi smoke test for FocusField sensors.

What it does:
  - Lists audio input devices and tries to capture a short 8ch snippet.
  - Opens each configured camera (device_path/device_index) and grabs one frame.
  - Runs the full pipeline for ~10s and writes enhanced.wav via file sink.

Run:
  python3 scripts/pi_smoke.py --config configs/full_3cam_8mic_pi.yaml
"""

from __future__ import annotations

import argparse
import threading
import time

import numpy as np

from focusfield.audio.devices import list_input_devices, resolve_input_device_index
from focusfield.core.artifacts import create_run_dir, write_run_metadata
from focusfield.core.bus import Bus
from focusfield.core.config import load_config
from focusfield.core.health import start_health_monitor
from focusfield.core.log_sink import start_log_sink
from focusfield.core.logging import LogEmitter
from focusfield.core.perf_monitor import start_perf_monitor

from focusfield.audio.capture import start_audio_capture
from focusfield.audio.doa.srp_phat import start_srp_phat
from focusfield.audio.vad import start_audio_vad
from focusfield.audio.beamform.mvdr import start_mvdr
from focusfield.audio.beamform.delay_and_sum import start_delay_and_sum
from focusfield.audio.enhance.denoise import start_denoise
from focusfield.audio.output.sink import start_output_sink
from focusfield.audio.sync.drift_check import start_drift_check
from focusfield.bench.replay.recorder import start_trace_recorder
from focusfield.fusion.av_association import start_av_association
from focusfield.fusion.lock_state_machine import start_lock_state_machine
from focusfield.vision.cameras import start_cameras
from focusfield.vision.speaker_heatmap import start_speaker_heatmap
from focusfield.vision.tracking.face_track import start_face_tracking


def main() -> None:
    parser = argparse.ArgumentParser(description="FocusField Pi smoke")
    parser.add_argument("--config", default="configs/full_3cam_8mic_pi.yaml")
    parser.add_argument("--run-seconds", type=float, default=10.0)
    args = parser.parse_args()

    config = load_config(args.config)

    runtime = config.setdefault("runtime", {})
    artifacts = runtime.setdefault("artifacts", {})
    base_dir = str(artifacts.get("dir", "artifacts"))
    retention = artifacts.get("retention", {})
    if not isinstance(retention, dict):
        retention = {}
    max_runs = int(retention.get("max_runs", 10))
    run_dir = create_run_dir(base_dir, run_id=str(runtime.get("run_id", "") or ""), max_runs=max_runs)
    runtime["run_id"] = run_dir.name
    artifacts["dir_run"] = str(run_dir)
    write_run_metadata(run_dir, config)

    print("=== Audio devices ===")
    for d in list_input_devices():
        print(f"[{d.index}] {d.name} (in={d.max_input_channels})")
    sel = resolve_input_device_index(config)
    print(f"Selected input device index: {sel}")
    print(f"Run dir: {run_dir}")

    bus = Bus(max_queue_depth=int(config.get("bus", {}).get("max_queue_depth", 8)))
    logger = LogEmitter(bus, min_level=config.get("logging", {}).get("level", "info"), run_id=str(runtime.get("run_id", "")))
    stop_event = threading.Event()

    threads = []

    log_thread = start_log_sink(bus, config, logger, stop_event)
    if log_thread is not None:
        threads.append(log_thread)
    health_thread = start_health_monitor(bus, config, logger, stop_event)
    if health_thread is not None:
        threads.append(health_thread)
    perf_thread = start_perf_monitor(bus, config, logger, stop_event)
    if perf_thread is not None:
        threads.append(perf_thread)
    drift_thread = start_drift_check(bus, config, logger, stop_event)
    if drift_thread is not None:
        threads.append(drift_thread)

    threads.append(start_audio_capture(bus, config, logger, stop_event))
    vad = start_audio_vad(bus, config, logger, stop_event)
    if vad is not None:
        threads.append(vad)
    doa = start_srp_phat(bus, config, logger, stop_event)
    if doa is not None:
        threads.append(doa)

    threads.extend(start_cameras(bus, config, logger, stop_event))
    threads.append(start_face_tracking(bus, config, logger, stop_event))
    threads.append(start_speaker_heatmap(bus, config, logger, stop_event))
    threads.append(start_av_association(bus, config, logger, stop_event))
    threads.append(start_lock_state_machine(bus, config, logger, stop_event))

    beam = start_mvdr(bus, config, logger, stop_event)
    if beam is None:
        beam = start_delay_and_sum(bus, config, logger, stop_event)
    if beam is not None:
        threads.append(beam)
    denoise = start_denoise(bus, config, logger, stop_event)
    if denoise is not None:
        threads.append(denoise)

    # Prefer trace recorder for audio artifacts + thumbnails.
    trace = start_trace_recorder(bus, config, logger, stop_event)
    if trace is not None:
        threads.append(trace)

    sink = start_output_sink(bus, config, logger, stop_event)
    if sink is not None:
        threads.append(sink)

    print("=== Running pipeline ===")
    t0 = time.time()

    q_frames = bus.subscribe("audio.frames")
    q_final = bus.subscribe("audio.enhanced.final")
    raw_seen = False
    final_seen = False

    cameras = [cam.get("id", f"cam{idx}") for idx, cam in enumerate(config.get("video", {}).get("cameras", []))]
    cam_queues = {cam_id: bus.subscribe(f"vision.frames.{cam_id}") for cam_id in cameras}
    cam_seen = {cam_id: False for cam_id in cameras}

    while time.time() - t0 < args.run_seconds:
        try:
            msg = q_frames.get(timeout=0.2)
            x = np.asarray(msg.get("data"))
            if x.ndim == 2 and x.shape[1] >= 2:
                raw_seen = True
        except Exception:
            pass
        try:
            msg2 = q_final.get(timeout=0.01)
            y = np.asarray(msg2.get("data"))
            if y.ndim == 1 and y.size > 0:
                final_seen = True
        except Exception:
            pass

        for cam_id, q in cam_queues.items():
            if cam_seen.get(cam_id):
                continue
            try:
                frame_msg = q.get_nowait()
                if frame_msg.get("data") is not None:
                    cam_seen[cam_id] = True
            except Exception:
                continue

    stop_event.set()
    print(f"Raw audio seen: {raw_seen}")
    print(f"Final audio seen: {final_seen}")
    if cam_seen:
        print(f"Camera frames seen: {cam_seen}")
    print(f"Artifacts: {run_dir}")


if __name__ == "__main__":
    main()

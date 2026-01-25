"""
CONTRACT: docs/11_contract_index.md
ROLE: Orchestration entrypoint for the FocusField pipeline.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: log.events  Type: LogEvent

CONFIG KEYS:
  - runtime.mode: selected run mode (mvp/full/bench/replay)
  - runtime.config_path: path to YAML config

PERF / TIMING:
  - start modules in defined order; stop in reverse order

FAILURE MODES:
  - module start failure -> stop pipeline -> log module_failed

LOG EVENTS:
  - module=main.run, event=module_failed, payload keys=module, error

TESTS:
  - tests/contract_tests.md must cover startup invariants
"""

from __future__ import annotations

import argparse
import threading
import time
from typing import List

from focusfield.core.bus import Bus
from focusfield.core.config import load_config
from focusfield.core.logging import LogEmitter
from focusfield.audio.capture import start_audio_capture
from focusfield.audio.vad import start_audio_vad
from focusfield.fusion.av_association import start_av_association
from focusfield.fusion.lock_state_machine import start_lock_state_machine
from focusfield.ui.server import start_ui_server
from focusfield.ui.telemetry import start_telemetry
from focusfield.vision.cameras import start_cameras
from focusfield.vision.speaker_heatmap import start_speaker_heatmap
from focusfield.vision.tracking.face_track import start_face_tracking


def main() -> None:
    parser = argparse.ArgumentParser(description="FocusField vision-first runner")
    parser.add_argument("--config", default="configs/mvp_1cam_4mic.yaml", help="Path to YAML config")
    parser.add_argument("--mode", default="vision", help="Run mode (vision)")
    args = parser.parse_args()

    config = load_config(args.config)
    bus = Bus(max_queue_depth=int(config.get("bus", {}).get("max_queue_depth", 8)))
    logger = LogEmitter(bus, min_level=config.get("logging", {}).get("level", "info"))
    stop_event = threading.Event()

    threads: List[threading.Thread] = []
    if args.mode not in {"vision"}:
        logger.emit("error", "main.run", "invalid_mode", {"mode": args.mode})
        raise SystemExit(f"Unsupported mode: {args.mode}")

    audio_thread = start_audio_capture(bus, config, logger, stop_event)
    if audio_thread is not None:
        threads.append(audio_thread)
    vad_thread = start_audio_vad(bus, config, logger, stop_event)
    if vad_thread is not None:
        threads.append(vad_thread)

    threads.extend(start_cameras(bus, config, logger, stop_event))
    threads.append(start_face_tracking(bus, config, logger, stop_event))
    threads.append(start_speaker_heatmap(bus, config, logger, stop_event))
    threads.append(start_av_association(bus, config, logger, stop_event))
    threads.append(start_lock_state_machine(bus, config, logger, stop_event))
    threads.append(start_telemetry(bus, config, logger, stop_event))
    threads.append(start_ui_server(bus, config, logger, stop_event))

    logger.emit("info", "main.run", "started", {"mode": args.mode})
    try:
        while not stop_event.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        logger.emit("info", "main.run", "shutdown", {})
        stop_event.set()


if __name__ == "__main__":
    main()

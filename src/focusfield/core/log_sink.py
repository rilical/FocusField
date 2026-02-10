"""focusfield.core.log_sink

CONTRACT: docs/11_contract_index.md
ROLE: Persist structured LogEvent messages to disk as JSONL.

INPUTS:
  - Topic: log.events  Type: LogEvent
OUTPUTS:
  - artifacts/<run_id>/logs/events.jsonl

CONFIG KEYS:
  - logging.file.enabled: enable file logging
  - logging.file.flush_interval_ms: flush interval
  - logging.file.rotate_mb: optional rotation size
  - runtime.artifacts.dir_run: run directory path
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


def start_log_sink(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    file_cfg = config.get("logging", {}).get("file", {})
    if not isinstance(file_cfg, dict):
        file_cfg = {}
    if not bool(file_cfg.get("enabled", False)):
        return None

    flush_interval_ms = float(file_cfg.get("flush_interval_ms", 200.0))
    rotate_mb = float(file_cfg.get("rotate_mb", 0.0))
    run_dir = config.get("runtime", {}).get("artifacts", {}).get("dir_run")
    if not run_dir:
        return None

    logs_dir = Path(str(run_dir)) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / "events.jsonl"
    q = bus.subscribe("log.events")

    def _run() -> None:
        fh = open(path, "a", encoding="utf-8")
        next_flush = time.time() + (flush_interval_ms / 1000.0)
        file_index = 0
        try:
            while not stop_event.is_set():
                try:
                    event = q.get(timeout=0.1)
                except queue.Empty:
                    event = None
                if event is not None:
                    fh.write(json.dumps(event, sort_keys=True) + "\n")
                now = time.time()
                if now >= next_flush:
                    fh.flush()
                    next_flush = now + (flush_interval_ms / 1000.0)
                    if rotate_mb > 0 and fh.tell() >= int(rotate_mb * 1024 * 1024):
                        fh.close()
                        file_index += 1
                        rotated = logs_dir / f"events.{file_index:03d}.jsonl"
                        try:
                            path.rename(rotated)
                        except Exception:  # noqa: BLE001
                            pass
                        fh = open(path, "a", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.emit("warning", "core.log_sink", "log_write_failed", {"path": str(path), "error": str(exc)})
        finally:
            try:
                fh.flush()
                fh.close()
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=_run, name="log-sink", daemon=True)
    thread.start()
    return thread

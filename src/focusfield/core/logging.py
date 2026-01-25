"""
CONTRACT: inline (source: src/focusfield/core/logging.md)
ROLE: Structured logging to JSONL + console.

INPUTS:
  - Topic: log.events  Type: LogEvent
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - logging.level: minimum level
  - logging.output_dir: log folder path
  - logging.rotate_mb: rotate size

PERF / TIMING:
  - asynchronous flush to avoid blocking

FAILURE MODES:
  - log write failure -> stderr fallback -> log log_write_failed

LOG EVENTS:
  - module=core.logging, event=log_write_failed, payload keys=path, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/core/logging.md):
# Logging contract

- Structured LogEvent with module, severity, and context.
- Events emitted for invariant violations.
- Logs are written to disk in FocusBench runs.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from focusfield.core.clock import now_ns


LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}


class LogEmitter:
    """Emit structured LogEvents to the bus and stdout."""

    def __init__(self, bus: Optional[Any], min_level: str = "info") -> None:
        self._bus = bus
        self._min_level = LEVELS.get(min_level, 20)

    def emit(self, level: str, module: str, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        record = {
            "t_ns": now_ns(),
            "level": level,
            "message": event,
            "context": {
                "module": module,
                "event": event,
                "details": payload or {},
            },
        }
        if self._bus is not None:
            self._bus.publish("log.events", record)
        if LEVELS.get(level, 0) >= self._min_level:
            print(json.dumps(record, sort_keys=True))

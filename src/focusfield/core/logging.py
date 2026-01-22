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

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

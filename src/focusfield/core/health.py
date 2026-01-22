"""
CONTRACT: inline (source: src/focusfield/core/health.md)
ROLE: Heartbeat aggregation and health summary.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - health.heartbeat_ms: expected heartbeat interval

PERF / TIMING:
  - update at heartbeat interval

FAILURE MODES:
  - missing heartbeat -> mark degraded -> log module_unhealthy

LOG EVENTS:
  - module=core.health, event=module_unhealthy, payload keys=module, last_seen_ms

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/core/health.md):
# Health contract

- Heartbeat from each module.
- Aggregated status with degraded mode indicators.
- Expose last error and recovery hints.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

"""
CONTRACT: inline (source: src/focusfield/ui/views/live.md)
ROLE: Live dashboard view rendering.

INPUTS:
  - Topic: ui.telemetry  Type: TelemetrySnapshot
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - ui.views.live.enabled: enable live view

PERF / TIMING:
  - render per telemetry update

FAILURE MODES:
  - render error -> log render_failed

LOG EVENTS:
  - module=ui.views.live, event=render_failed, payload keys=error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/ui/views/live.md):
# Live view

- Camera tiles (1 or 3) with face overlays.
- Polar heatmap visualization.
- Lock state and event log.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

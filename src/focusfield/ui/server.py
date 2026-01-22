"""
CONTRACT: inline (source: src/focusfield/ui/server.md)
ROLE: HTTP + WebSocket server for UI.

INPUTS:
  - Topic: ui.telemetry  Type: TelemetrySnapshot
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - ui.host: bind host
  - ui.port: bind port

PERF / TIMING:
  - serve at localhost; stable ws updates

FAILURE MODES:
  - bind failure -> log bind_failed -> exit

LOG EVENTS:
  - module=ui.server, event=bind_failed, payload keys=host, port, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/ui/server.md):
# UI server

- HTTP + WebSocket contract.
- Serve live and bench views.
- Stream telemetry at a stable update rate.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

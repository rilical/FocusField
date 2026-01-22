"""
CONTRACT: inline (source: src/focusfield/ui/telemetry.md)
ROLE: Aggregate telemetry for UI.

INPUTS:
  - Topic: audio.doa_heatmap  Type: DoaHeatmap
  - Topic: vision.face_tracks  Type: FaceTrack[]
  - Topic: fusion.target_lock  Type: TargetLock
  - Topic: log.events  Type: LogEvent
OUTPUTS:
  - Topic: ui.telemetry  Type: TelemetrySnapshot

CONFIG KEYS:
  - ui.telemetry_hz: update rate

PERF / TIMING:
  - stable update rate

FAILURE MODES:
  - missing inputs -> partial telemetry -> log telemetry_partial

LOG EVENTS:
  - module=ui.telemetry, event=telemetry_partial, payload keys=missing

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/ui/telemetry.md):
# Telemetry contract

- Compact merged state for UI.
- Includes heatmap, lock state, and face summaries.
- Versioned for forward compatibility.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

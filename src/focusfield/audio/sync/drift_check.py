"""
CONTRACT: inline (source: src/focusfield/audio/sync/drift_check.md)
ROLE: Detect drift between channels or timestamps.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - audio.sync.max_drift_ms: drift threshold

PERF / TIMING:
  - periodic checks while running

FAILURE MODES:
  - drift exceeded -> log drift_exceeded

LOG EVENTS:
  - module=audio.drift_check, event=drift_exceeded, payload keys=drift_ms

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/sync/drift_check.md):
# Drift check

- Detect timestamp drift between channels.
- Compare expected vs observed frame intervals.
- Emit LogEvent when drift exceeds threshold.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

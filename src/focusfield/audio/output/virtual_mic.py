"""
CONTRACT: inline (source: src/focusfield/audio/output/virtual_mic.md)
ROLE: Virtual mic routing placeholder.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - output.virtual_mic.device_name: OS device name

PERF / TIMING:
  - n/a

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=<name>, event=not_implemented, payload keys=reason

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/output/virtual_mic.md):
# Virtual mic routing (no code)

- OS-specific routing plan for virtual mic output.
- Document device names and expected sample rate.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

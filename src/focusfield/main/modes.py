"""
CONTRACT: inline (source: src/focusfield/main/modes.md)
ROLE: Define run modes and mode metadata.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - runtime.mode: selected run mode (mvp/full/bench/replay)

PERF / TIMING:
  - n/a

FAILURE MODES:
  - unknown mode -> raise error -> log event=invalid_mode

LOG EVENTS:
  - module=main.modes, event=invalid_mode, payload keys=mode

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/main/modes.md):
# Run modes

- mvp: 1 camera + 4 mic configuration.
- full: 3 cameras + 8 mic configuration.
- bench: FocusBench replay mode.
- replay: play back recorded sessions.
- lab_debug: verbose logging and validation.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

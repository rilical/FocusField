"""
CONTRACT: inline (source: src/focusfield/audio/doa/geometry.md)
ROLE: Array geometry helpers and steering vectors.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - audio.device_profile: geometry source

PERF / TIMING:
  - precompute steering tables

FAILURE MODES:
  - invalid geometry -> raise -> log geometry_invalid

LOG EVENTS:
  - module=audio.doa.geometry, event=geometry_invalid, payload keys=reason

TESTS:
  - tests/contract_tests.md must cover geometry validation

CONTRACT DETAILS (inline from src/focusfield/audio/doa/geometry.md):
# Array geometry

- Supported geometry formats and units.
- Define steering vector assumptions.
- Validate geometry matches channel count.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

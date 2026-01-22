"""
CONTRACT: inline (source: src/focusfield/fusion/target_output.md)
ROLE: Normalize TargetLock output and reason strings.

INPUTS:
  - Topic: fusion.candidates  Type: AssociationCandidate[]
OUTPUTS:
  - Topic: fusion.target_lock  Type: TargetLock

CONFIG KEYS:
  - fusion.reason_detail_level: verbosity

PERF / TIMING:
  - per update

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=<name>, event=not_implemented, payload keys=reason

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/fusion/target_output.md):
# Target output

- Emit TargetLock with state, angle, and confidence.
- Provide reason string and candidate summary.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

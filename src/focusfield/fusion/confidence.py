"""
CONTRACT: inline (source: src/focusfield/fusion/confidence.md)
ROLE: Combine candidate scores into confidence.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - fusion.score_weights: component weights

PERF / TIMING:
  - per candidate

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=<name>, event=not_implemented, payload keys=reason

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/fusion/confidence.md):
# Confidence model

- Combine mouth activity, face confidence, DOA peak score.
- Penalize large angular distance.
- Normalize to 0..1 for lock logic.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

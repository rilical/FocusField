"""
CONTRACT: inline (source: src/focusfield/vision/calibration/hfov_estimation.md)
ROLE: Estimate camera HFOV from calibration.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - vision.calibration.pattern_size: calibration pattern size

PERF / TIMING:
  - offline calibration step

FAILURE MODES:
  - fit error -> log hfov_fit_failed

LOG EVENTS:
  - module=vision.calibration.hfov_estimation, event=hfov_fit_failed, payload keys=error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/vision/calibration/hfov_estimation.md):
# HFOV estimation

- Estimate HFOV using calibration targets.
- Validate against expected camera profile.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

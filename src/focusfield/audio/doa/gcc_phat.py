"""
CONTRACT: inline (source: src/focusfield/audio/doa/gcc_phat.md)
ROLE: Baseline GCC-PHAT DOA estimator.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
OUTPUTS:
  - Topic: audio.doa_heatmap  Type: DoaHeatmap

CONFIG KEYS:
  - audio.doa.gcc_phat_enabled: enable baseline

PERF / TIMING:
  - low-rate output for debugging

FAILURE MODES:
  - estimation error -> log doa_failed

LOG EVENTS:
  - module=audio.doa.gcc_phat, event=doa_failed, payload keys=error

TESTS:
  - tests/audio_doa_sanity.md must cover baseline behavior

CONTRACT DETAILS (inline from src/focusfield/audio/doa/gcc_phat.md):
# GCC-PHAT baseline

- Optional baseline for DOA estimation.
- Used for sanity checks and debugging.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

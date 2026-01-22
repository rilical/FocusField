"""
CONTRACT: inline (source: src/focusfield/audio/beamform/mvdr.md)
ROLE: MVDR beamformer (stretch).

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
  - Topic: fusion.target_lock  Type: TargetLock
OUTPUTS:
  - Topic: audio.enhanced.beamformed  Type: EnhancedAudio

CONFIG KEYS:
  - audio.beamformer.method: mvdr
  - audio.beamformer.mvdr_enabled: enable MVDR

PERF / TIMING:
  - heavier compute; measure latency

FAILURE MODES:
  - unstable weights -> fall back to delay_and_sum -> log mvdr_unstable

LOG EVENTS:
  - module=audio.beamform.mvdr, event=mvdr_unstable, payload keys=reason

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/beamform/mvdr.md):
# MVDR beamformer

- Stretch contract for advanced suppression.
- Requires noise covariance estimation.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

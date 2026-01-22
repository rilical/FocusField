"""
CONTRACT: inline (source: src/focusfield/audio/beamform/delay_and_sum.md)
ROLE: Delay-and-sum beamformer.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
  - Topic: fusion.target_lock  Type: TargetLock
OUTPUTS:
  - Topic: audio.enhanced.beamformed  Type: EnhancedAudio

CONFIG KEYS:
  - audio.beamformer.method: delay_and_sum
  - audio.beamformer.use_last_lock_ms: hold last target
  - audio.beamformer.no_lock_behavior: omni|mute|last_lock

PERF / TIMING:
  - bounded latency; buffer size set by block_size

FAILURE MODES:
  - missing lock -> fall back to omni -> log no_lock
  - missing geometry -> fall back to omni -> log geometry_missing

LOG EVENTS:
  - module=audio.beamform.delay_and_sum, event=no_lock, payload keys=behavior
  - module=audio.beamform.delay_and_sum, event=geometry_missing, payload keys=profile

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/beamform/delay_and_sum.md):
# Delay-and-sum beamformer

- MVP beamformer contract.
- Steering angle from TargetLock.
- NO_LOCK outputs omni baseline.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

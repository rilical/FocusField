"""
CONTRACT: inline (source: src/focusfield/audio/enhance/denoise.md)
ROLE: Optional denoise stage.

INPUTS:
  - Topic: audio.enhanced.beamformed  Type: EnhancedAudio
OUTPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio

CONFIG KEYS:
  - audio.denoise.enabled: enable denoise
  - audio.denoise.backend: rnnoise|webrtc

PERF / TIMING:
  - keep latency within budget

FAILURE MODES:
  - backend error -> bypass -> log denoise_failed

LOG EVENTS:
  - module=audio.enhance.denoise, event=denoise_failed, payload keys=backend, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/enhance/denoise.md):
# Denoise wrapper

- Optional denoise stage (RNNoise or WebRTC).
- Must preserve sample rate and frame size.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

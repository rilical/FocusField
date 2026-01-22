"""
CONTRACT: inline (source: src/focusfield/audio/enhance/agc_post.md)
ROLE: Optional post-AGC.

INPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio
OUTPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio

CONFIG KEYS:
  - audio.agc_post.enabled: enable post AGC
  - audio.agc_post.target_rms: target RMS

PERF / TIMING:
  - per-frame gain adjustment

FAILURE MODES:
  - clipping detected -> clamp -> log clipping

LOG EVENTS:
  - module=audio.enhance.agc_post, event=clipping, payload keys=rms

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/enhance/agc_post.md):
# Post-AGC

- Optional post gain control.
- Clamp to avoid clipping.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

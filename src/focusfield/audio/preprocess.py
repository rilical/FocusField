"""
CONTRACT: inline (source: src/focusfield/audio/preprocess.md)
ROLE: Optional VAD/HPF/AGC conditioning.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
OUTPUTS:
  - Topic: audio.frames  Type: AudioFrame

CONFIG KEYS:
  - audio.preprocess.vad_enabled: enable VAD
  - audio.preprocess.hpf_hz: high-pass cutoff
  - audio.preprocess.agc_enabled: enable AGC

PERF / TIMING:
  - per-frame processing; no extra buffering

FAILURE MODES:
  - processing error -> bypass -> log preprocess_failed

LOG EVENTS:
  - module=audio.preprocess, event=preprocess_failed, payload keys=stage, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/preprocess.md):
# Audio preprocess

- VAD, HPF, and optional AGC.
- Each stage emits stats for debugging.
- Must preserve channel order.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

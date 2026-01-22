"""
CONTRACT: inline (source: src/focusfield/audio/capture.md)
ROLE: Produce AudioFrame blocks on audio.frames.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: audio.frames  Type: AudioFrame

CONFIG KEYS:
  - audio.channels: channel count
  - audio.sample_rate_hz: sample rate
  - audio.block_size: frames per block
  - audio.device_profile: mic array profile

PERF / TIMING:
  - fixed cadence at block_size/sample_rate_hz
  - stable seq increments

FAILURE MODES:
  - device disconnect -> reconnect or stop -> log disconnect

LOG EVENTS:
  - module=audio.capture, event=disconnect, payload keys=device_id
  - module=audio.capture, event=underrun, payload keys=frames_dropped

TESTS:
  - tests/contract_tests.md must cover seq monotonicity

CONTRACT DETAILS (inline from src/focusfield/audio/capture.md):
# Audio capture

- Output AudioFrame with fixed block size.
- Maintain monotonic seq and t_ns.
- Expose overflow or underrun events.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

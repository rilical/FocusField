"""
CONTRACT: inline (source: src/focusfield/adapters/audio_backend.md)
ROLE: Audio backend abstraction for capture.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - audio.device_id: OS device identifier
  - audio.channels: channel count
  - audio.sample_rate_hz: sample rate
  - audio.block_size: frames per block

PERF / TIMING:
  - real-time capture; stable callback cadence

FAILURE MODES:
  - device error -> raise -> log device_error

LOG EVENTS:
  - module=adapters.audio_backend, event=device_error, payload keys=device_id, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/adapters/audio_backend.md):
# Audio backend abstraction

- Provide a uniform capture interface.
- Support sounddevice, pyaudio, or OS-specific backends.
- Surface buffer underruns and overruns as LogEvent.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

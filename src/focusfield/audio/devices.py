"""
CONTRACT: inline (source: src/focusfield/audio/devices.md)
ROLE: Enumerate audio devices and resolve selected device.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - audio.device_id: preferred device id (optional)
  - audio.channels: required channels

PERF / TIMING:
  - enumerate once at startup

FAILURE MODES:
  - no matching device -> raise -> log device_not_found

LOG EVENTS:
  - module=audio.devices, event=device_not_found, payload keys=channels

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/devices.md):
# Audio device enumeration

- Enumerate devices and channel counts.
- Map stable channel order to logical channels.
- Log selected device and profile.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

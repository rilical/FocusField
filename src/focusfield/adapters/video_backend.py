"""
CONTRACT: inline (source: src/focusfield/adapters/video_backend.md)
ROLE: Video backend abstraction for multi-camera capture.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - video.cameras[].device_index: camera index
  - video.cameras[].width: frame width
  - video.cameras[].height: frame height
  - video.cameras[].fps: frame rate

PERF / TIMING:
  - frame timestamps per capture

FAILURE MODES:
  - device error -> mark degraded -> log device_error

LOG EVENTS:
  - module=adapters.video_backend, event=device_error, payload keys=camera_id, error

TESTS:
  - tests/usb_bandwidth_sanity.md must cover multi-camera capture

CONTRACT DETAILS (inline from src/focusfield/adapters/video_backend.md):
# Video backend abstraction

- Provide a uniform capture interface for multiple cameras.
- Support OpenCV or platform-specific capture.
- Surface dropped frames and device errors.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

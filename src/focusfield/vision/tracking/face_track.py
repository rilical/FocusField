"""
CONTRACT: inline (source: src/focusfield/vision/tracking/face_track.md)
ROLE: Face detection and tracking with stable IDs.

INPUTS:
  - Topic: vision.frames.cam0  Type: VideoFrame
  - Topic: vision.frames.cam1  Type: VideoFrame
  - Topic: vision.frames.cam2  Type: VideoFrame
OUTPUTS:
  - Topic: vision.face_tracks  Type: FaceTrack[]

CONFIG KEYS:
  - vision.face.min_confidence: minimum detection confidence
  - vision.track.max_missing_frames: drop threshold

PERF / TIMING:
  - per-frame detection/tracking

FAILURE MODES:
  - detector failure -> log detector_failed

LOG EVENTS:
  - module=vision.face_track, event=detector_failed, payload keys=error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/vision/tracking/face_track.md):
# Face tracking

- Detect faces per camera and assign stable track_id.
- Provide bbox, confidence, and bearing_deg.
- Merge tracks across cameras into global list.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

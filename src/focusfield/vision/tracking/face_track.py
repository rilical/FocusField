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

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import json

import cv2

from focusfield.core.clock import now_ns
from focusfield.vision.calibration.bearing import bearing_from_bbox
from focusfield.vision.mouth.mouth_activity import FaceMeshMouthEstimator, MouthActivityEstimator
from focusfield.vision.mouth.thresholds import SpeakingHysteresis
from focusfield.vision.tracking.track_smoothing import TrackSmoother


BBox = Tuple[int, int, int, int]


class CameraTracker:
    """Per-camera face detection, tracking, and mouth activity."""

    def __init__(self, camera_id: str, config: Dict[str, Any], camera_cfg: Dict[str, Any], logger: Any) -> None:
        self._camera_id = camera_id
        vision_cfg = config.get("vision", {})
        face_cfg = vision_cfg.get("face", {})
        track_cfg = vision_cfg.get("track", {})
        mouth_cfg = config.get("vision", {}).get("mouth", {})
        thresholds = config.get("fusion", {}).get("thresholds", {})
        self._min_area = int(face_cfg.get("min_area", 800))
        self._min_neighbors = int(face_cfg.get("min_neighbors", 6))
        self._scale_factor = float(face_cfg.get("scale_factor", 1.2))
        self._detect_width = int(face_cfg.get("detect_width", 360))
        self._detect_every_n = max(1, int(face_cfg.get("detect_every_n", 1)))
        self._min_age_frames = int(track_cfg.get("min_age_frames", 2))
        self._smoother = TrackSmoother(
            iou_threshold=float(face_cfg.get("iou_threshold", 0.3)),
            max_missing_frames=int(track_cfg.get("max_missing_frames", face_cfg.get("max_missing_frames", 10))),
            smoothing_alpha=float(track_cfg.get("smoothing_alpha", 0.6)),
        )
        self._mouth = MouthActivityEstimator(
            smoothing_alpha=float(mouth_cfg.get("smoothing_alpha", 0.75)),
            min_activity=float(mouth_cfg.get("min_activity", 0.08)),
            max_activity=float(mouth_cfg.get("max_activity", 0.4)),
            diff_threshold=float(mouth_cfg.get("diff_threshold", 12.0)),
        )
        self._logger = logger
        self._mesh = None
        self._mesh_step = max(1, int(mouth_cfg.get("mesh_every_n", 1)))
        self._mesh_edge_margin = float(mouth_cfg.get("mesh_edge_margin", 0.08))
        if mouth_cfg.get("use_facemesh", True):
            try:
                self._mesh = FaceMeshMouthEstimator(
                    max_faces=int(mouth_cfg.get("mesh_max_faces", 5)),
                    min_detection_confidence=float(mouth_cfg.get("mesh_min_detection_confidence", 0.5)),
                    min_tracking_confidence=float(mouth_cfg.get("mesh_min_tracking_confidence", 0.5)),
                    min_activity=float(mouth_cfg.get("mesh_min_activity", 0.005)),
                    max_activity=float(mouth_cfg.get("mesh_max_activity", 0.1)),
                    model_path=mouth_cfg.get("mesh_model_path"),
                )
            except Exception as exc:  # noqa: BLE001
                self._mesh = None
                self._logger.emit("warning", "vision.mouth_activity", "facemesh_unavailable", {"error": str(exc)})
        self._mesh_frame_count = 0
        speak_on = float(thresholds.get("speak_on_threshold", 0.5))
        speak_off = float(thresholds.get("speak_off_threshold", 0.4))
        self._speak_on = speak_on
        self._speak_off = speak_off
        self._speak_on_frames = int(thresholds.get("min_on_frames", 3))
        self._speak_off_frames = int(thresholds.get("min_off_frames", 3))
        self._speaking: Dict[int, SpeakingHysteresis] = {}
        self._camera_cfg = camera_cfg
        self._cascade = self._load_face_cascade()
        self._frame_count = 0
        self._last_detections: List[Tuple[BBox, float]] = []
        self._bearing_model = str(camera_cfg.get("bearing_model", "linear")).lower()
        self._bearing_lut = _load_bearing_lut(camera_cfg.get("bearing_lut_path"), camera_id, logger)

    def process_frame(self, frame_msg: Dict[str, Any]) -> List[Dict[str, Any]]:
        frame = frame_msg["data"]
        height, width = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self._frame_count += 1
        if self._frame_count % self._detect_every_n == 0 or not self._last_detections:
            detections = self._detect_faces(gray)
            self._last_detections = detections
        else:
            detections = self._last_detections
        tracks = self._smoother.update(detections)
        mesh_faces = []
        if self._mesh is not None:
            self._mesh_frame_count += 1
            if self._mesh_frame_count % self._mesh_step == 0:
                mesh_faces = self._mesh.estimate(frame)
        if self._mesh is not None and not mesh_faces:
            self._logger.emit("debug", "vision.mouth_activity", "facemesh_no_faces", {"camera_id": self._camera_id})
        active_ids = {track.track_id for track in tracks}
        for track_id in list(self._speaking.keys()):
            if track_id not in active_ids:
                self._speaking.pop(track_id, None)
                self._mouth.drop(f"{self._camera_id}-{track_id}")

        output_tracks: List[Dict[str, Any]] = []
        for track in tracks:
            if not track.matched:
                continue
            if track.age_frames < self._min_age_frames:
                continue
            bbox = _bbox_from_float(track.smooth_bbox)
            if _bbox_area(bbox) < self._min_area:
                continue
            track_key = f"{self._camera_id}-{track.track_id}"
            mesh_match = _match_mesh_face(bbox, mesh_faces)
            if mesh_match is not None and not _near_frame_edge(bbox, width, height, self._mesh_edge_margin):
                activity = self._mouth.smooth(track_key, float(mesh_match.get("activity", 0.0)))
            else:
                activity = self._mouth.compute(track_key, frame, bbox)
            speaking_tracker = self._speaking.get(track.track_id)
            if speaking_tracker is None:
                speaking_tracker = SpeakingHysteresis(
                    speak_on_threshold=self._speak_on,
                    speak_off_threshold=self._speak_off,
                    min_on_frames=self._speak_on_frames,
                    min_off_frames=self._speak_off_frames,
                )
                self._speaking[track.track_id] = speaking_tracker
            speaking = speaking_tracker.update(activity)
            bearing = bearing_from_bbox(
                bbox=bbox,
                frame_width=width,
                hfov_deg=float(self._camera_cfg.get("hfov_deg", 90.0)),
                yaw_offset_deg=float(self._camera_cfg.get("yaw_offset_deg", 0.0)),
                bearing_offset_deg=float(self._camera_cfg.get("bearing_offset_deg", 0.0)),
                bearing_model=self._bearing_model,
                bearing_lut=self._bearing_lut,
            )
            output_tracks.append(
                {
                    "t_ns": frame_msg.get("t_ns", now_ns()),
                    "seq": frame_msg.get("seq", 0),
                    "track_id": track_key,
                    "bbox": {"x": bbox[0], "y": bbox[1], "w": bbox[2], "h": bbox[3]},
                    "confidence": float(track.confidence),
                    "bearing_deg": bearing,
                    "mouth_activity": activity,
                    "speaking": speaking,
                    "camera_id": self._camera_id,
                }
            )
        return output_tracks

    def _load_face_cascade(self):
        candidates = []

        if hasattr(cv2, "data") and hasattr(cv2.data, "haarcascades"):
            candidates.append(Path(str(cv2.data.haarcascades)) / "haarcascade_frontalface_default.xml")

        cv2_root = Path(cv2.__file__).resolve().parent if cv2.__file__ else None
        if cv2_root is not None:
            candidates.extend(
                [
                    cv2_root / "data" / "haarcascade_frontalface_default.xml",
                    cv2_root.parent / "share" / "opencv4" / "haarcascades" / "haarcascade_frontalface_default.xml",
                ]
            )

        candidates.extend(
            [
                Path("/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"),
                Path("/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"),
                Path("/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml"),
            ]
        )

        for path in candidates:
            if not path.exists():
                continue
            cascade = cv2.CascadeClassifier(str(path))
            if not cascade.empty():
                return cascade

        self._logger.emit(
            "warning",
            "vision.face_track",
            "cascade_missing",
            {
                "camera_id": self._camera_id,
                "note": "Face cascade unavailable; running with face detection disabled. Install opencv-data or add haarcascade file manually.",
            },
        )
        return None

    def _detect_faces(self, gray_frame) -> List[Tuple[BBox, float]]:
        if self._cascade is None:
            return []
        height, width = gray_frame.shape[:2]
        scale = 1.0
        resized = gray_frame
        if self._detect_width > 0 and width > self._detect_width:
            scale = self._detect_width / float(width)
            resized = cv2.resize(gray_frame, (self._detect_width, int(height * scale)), interpolation=cv2.INTER_AREA)
        min_size = max(20, int((self._min_area ** 0.5) * scale))
        detections = self._cascade.detectMultiScale(
            resized,
            scaleFactor=self._scale_factor,
            minNeighbors=self._min_neighbors,
            minSize=(min_size, min_size),
        )
        results: List[Tuple[BBox, float]] = []
        for (x, y, w, h) in detections:
            if scale != 1.0:
                x = int(x / scale)
                y = int(y / scale)
                w = int(w / scale)
                h = int(h / scale)
            bbox = (int(x), int(y), int(w), int(h))
            if _bbox_area(bbox) < self._min_area:
                continue
            results.append((bbox, 1.0))
        return results


def start_face_tracking(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    cameras = config.get("video", {}).get("cameras", [])
    queues: Dict[str, queue.Queue] = {}
    trackers: Dict[str, CameraTracker] = {}
    latest_tracks: Dict[str, List[Dict[str, Any]]] = {}
    for index, cam_cfg in enumerate(cameras):
        camera_id = cam_cfg.get("id", f"cam{index}")
        topic = f"vision.frames.{camera_id}"
        queues[camera_id] = bus.subscribe(topic)
        trackers[camera_id] = CameraTracker(camera_id, config, cam_cfg, logger)
        latest_tracks[camera_id] = []

    def _run() -> None:
        while not stop_event.is_set():
            updated = False
            for camera_id, q in queues.items():
                try:
                    frame_msg = q.get(timeout=0.01)
                except queue.Empty:
                    continue
                try:
                    tracks = trackers[camera_id].process_frame(frame_msg)
                    latest_tracks[camera_id] = tracks
                    updated = True
                except Exception as exc:  # noqa: BLE001
                    logger.emit("error", "vision.face_track", "detector_failed", {"camera_id": camera_id, "error": str(exc)})
            if updated:
                merged: List[Dict[str, Any]] = []
                for tracks in latest_tracks.values():
                    merged.extend(tracks)
                bus.publish("vision.face_tracks", merged)

    thread = threading.Thread(target=_run, name="face-track", daemon=True)
    thread.start()
    return thread


def _bbox_area(bbox: BBox) -> int:
    return int(bbox[2] * bbox[3])


def _bbox_from_float(bbox: Tuple[float, float, float, float]) -> BBox:
    x, y, w, h = bbox
    return int(round(x)), int(round(y)), int(round(w)), int(round(h))


def _match_mesh_face(bbox: BBox, mesh_faces: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not mesh_faces:
        return None
    bx, by, bw, bh = bbox
    bcx, bcy = bx + bw / 2.0, by + bh / 2.0
    best = None
    best_dist = float("inf")
    for face in mesh_faces:
        face_bbox = face.get("bbox")
        if not face_bbox:
            continue
        fx, fy, fw, fh = face_bbox
        fcx, fcy = fx + fw / 2.0, fy + fh / 2.0
        dist = ((fcx - bcx) ** 2 + (fcy - bcy) ** 2) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best = face
    max_dim = max(bw, bh)
    if best is None or best_dist > max_dim * 0.75:
        return None
    return best


def _near_frame_edge(bbox: BBox, frame_width: int, frame_height: int, margin_ratio: float) -> bool:
    if frame_width <= 0 or frame_height <= 0:
        return True
    margin_x = int(frame_width * margin_ratio)
    margin_y = int(frame_height * margin_ratio)
    x, y, w, h = bbox
    if x <= margin_x or (x + w) >= (frame_width - margin_x):
        return True
    if y <= margin_y or (y + h) >= (frame_height - margin_y):
        return True
    return False


def _load_bearing_lut(path: Optional[str], camera_id: str, logger: Any) -> Optional[List[float]]:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list) and data:
            return [float(x) for x in data]
    except Exception as exc:  # noqa: BLE001
        logger.emit("warning", "vision.calibration.bearing", "calibration_missing", {"camera_id": camera_id, "error": str(exc)})
    return None


def _iou(box_a: BBox, box_b: BBox) -> float:
    ax, ay, aw, ah = box_a
    bx, by, bw, bh = box_b
    a_x2 = ax + aw
    a_y2 = ay + ah
    b_x2 = bx + bw
    b_y2 = by + bh

    inter_x1 = max(ax, bx)
    inter_y1 = max(ay, by)
    inter_x2 = min(a_x2, b_x2)
    inter_y2 = min(a_y2, b_y2)
    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0
    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area_a = aw * ah
    area_b = bw * bh
    union = area_a + area_b - inter_area
    if union <= 0:
        return 0.0
    return inter_area / union

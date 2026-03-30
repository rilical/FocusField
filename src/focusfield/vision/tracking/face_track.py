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
import time
import urllib.request
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import json

import cv2
import numpy as np

from focusfield.core.clock import now_ns
from focusfield.vision.calibration.bearing import bearing_from_bbox
from focusfield.vision.mouth.mouth_activity import FaceMeshMouthEstimator, MouthActivityEstimator, TFLiteMouthEstimator
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
            center_gate_px=float(track_cfg.get("center_gate_px", 180.0) or 180.0),
            velocity_alpha=float(track_cfg.get("velocity_alpha", 0.45) or 0.45),
        )
        self._mouth = MouthActivityEstimator(
            smoothing_alpha=float(mouth_cfg.get("smoothing_alpha", 0.75)),
            min_activity=float(mouth_cfg.get("min_activity", 0.08)),
            max_activity=float(mouth_cfg.get("max_activity", 0.4)),
            diff_threshold=float(mouth_cfg.get("diff_threshold", 12.0)),
        )
        self._logger = logger
        self._mesh = None
        self._tflite = None
        self._mouth_backend = str(mouth_cfg.get("backend", "auto") or "auto").strip().lower()
        self._mesh_step = max(1, int(mouth_cfg.get("mesh_every_n", 1)))
        self._mesh_edge_margin = float(mouth_cfg.get("mesh_edge_margin", 0.08))
        self._face_backend = str(face_cfg.get("backend", "auto") or "auto").strip().lower()
        self._yunet_score_threshold = float(face_cfg.get("yunet_score_threshold", face_cfg.get("min_confidence", 0.6)))
        self._yunet_nms_threshold = float(face_cfg.get("yunet_nms_threshold", 0.3))
        self._yunet_top_k = int(face_cfg.get("yunet_top_k", 8))
        self._yunet_model_path = face_cfg.get("yunet_model_path")
        self._init_mouth_model(mouth_cfg)
        self._mesh_frame_count = 0
        self._visual_quality_floor = float(mouth_cfg.get("visual_quality_floor", 0.2) or 0.2)
        self._visual_motion_weight = float(mouth_cfg.get("visual_motion_weight", 0.35) or 0.35)
        speak_on = float(thresholds.get("speak_on_threshold", 0.5))
        speak_off = float(thresholds.get("speak_off_threshold", 0.4))
        self._speak_on = speak_on
        self._speak_off = speak_off
        self._speak_on_frames = int(thresholds.get("min_on_frames", 3))
        self._speak_off_frames = int(thresholds.get("min_off_frames", 3))
        self._speaking: Dict[int, SpeakingHysteresis] = {}
        self._camera_cfg = camera_cfg
        self._detector_kind, self._detector = self._load_face_detector()
        self._yunet_input_size: Optional[Tuple[int, int]] = None
        self._frame_count = 0
        self._last_detections: List[Tuple[BBox, float]] = []
        self._bearing_model = str(camera_cfg.get("bearing_model", "linear")).lower()
        self._bearing_lut = _load_bearing_lut(camera_cfg.get("bearing_lut_path"), camera_id, logger)

    def apply_calibration(self, calibration: Dict[str, Any]) -> Dict[str, Any]:
        self._camera_cfg["yaw_offset_deg"] = float(calibration.get("yaw_offset_deg", self._camera_cfg.get("yaw_offset_deg", 0.0)) or 0.0)
        self._camera_cfg["bearing_offset_deg"] = float(
            calibration.get("bearing_offset_deg", self._camera_cfg.get("bearing_offset_deg", 0.0)) or 0.0
        )
        bearing_model = calibration.get("bearing_model")
        if bearing_model:
            self._bearing_model = str(bearing_model).lower()
            self._camera_cfg["bearing_model"] = self._bearing_model
        bearing_lut_path = calibration.get("bearing_lut_path")
        if isinstance(bearing_lut_path, str) and bearing_lut_path != self._camera_cfg.get("bearing_lut_path"):
            self._camera_cfg["bearing_lut_path"] = bearing_lut_path
            self._bearing_lut = _load_bearing_lut(bearing_lut_path, self._camera_id, self._logger)
        return {
            "camera_id": self._camera_id,
            "yaw_offset_deg": float(self._camera_cfg.get("yaw_offset_deg", 0.0) or 0.0),
            "bearing_offset_deg": float(self._camera_cfg.get("bearing_offset_deg", 0.0) or 0.0),
        }

    def process_frame(self, frame_msg: Dict[str, Any]) -> List[Dict[str, Any]]:
        frame = frame_msg["data"]
        height, width = frame.shape[:2]
        self._frame_count += 1
        if self._frame_count % self._detect_every_n == 0 or not self._last_detections:
            detections = self._detect_faces(frame)
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
            visual = self._estimate_visual_state(track_key, frame, bbox, width, height, mesh_faces)
            speaking_tracker = self._speaking.get(track.track_id)
            if speaking_tracker is None:
                speaking_tracker = SpeakingHysteresis(
                    speak_on_threshold=self._speak_on,
                    speak_off_threshold=self._speak_off,
                    min_on_frames=self._speak_on_frames,
                    min_off_frames=self._speak_off_frames,
                )
                self._speaking[track.track_id] = speaking_tracker
            speaking = speaking_tracker.update(float(visual.get("visual_speaking_prob", 0.0)))
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
                        "mouth_activity": float(visual.get("mouth_activity", 0.0)),
                        "visual_speaking_prob": float(visual.get("visual_speaking_prob", 0.0)),
                        "visual_quality": float(visual.get("visual_quality", 0.0)),
                        "motion_activity": float(visual.get("motion_activity", 0.0)),
                        "landmark_presence": float(visual.get("landmark_presence", 0.0)),
                        "visual_backend": str(visual.get("backend", "diff")),
                        "speaking": speaking,
                        "track_age_frames": int(track.age_frames),
                        "detector_backend": self._detector_kind,
                        "camera_id": self._camera_id,
                    }
                )
        return output_tracks

    def _load_face_detector(self):
        if self._face_backend in {"auto", "yunet"}:
            detector = self._load_yunet_detector()
            if detector is not None:
                return "yunet", detector
            if self._face_backend == "yunet":
                self._logger.emit(
                    "warning",
                    "vision.face_track",
                    "yunet_unavailable",
                    {"camera_id": self._camera_id, "note": "Falling back to Haar cascade."},
                )
        return "haar", self._load_face_cascade()

    def _init_mouth_model(self, mouth_cfg: Dict[str, Any]) -> None:
        use_facemesh = bool(mouth_cfg.get("use_facemesh", True))
        backend = self._mouth_backend
        if backend not in {"auto", "tflite", "facemesh", "diff"}:
            backend = "auto"

        if backend in {"auto", "tflite"}:
            try:
                self._tflite = TFLiteMouthEstimator(
                    min_activity=float(mouth_cfg.get("mesh_min_activity", 0.005)),
                    max_activity=float(mouth_cfg.get("mesh_max_activity", 0.1)),
                    model_path=mouth_cfg.get("tflite_model_path"),
                    task_path=mouth_cfg.get("mesh_model_path"),
                    model_member=str(mouth_cfg.get("tflite_model_member", "face_landmarks_detector.tflite") or "face_landmarks_detector.tflite"),
                    num_threads=int(mouth_cfg.get("tflite_threads", 1) or 1),
                    min_presence=float(mouth_cfg.get("tflite_min_presence", 0.0) or 0.0),
                    crop_scale=float(mouth_cfg.get("tflite_crop_scale", 1.45) or 1.45),
                )
                self._logger.emit("info", "vision.mouth_activity", "tflite_ready", {"camera_id": self._camera_id})
                return
            except Exception as exc:  # noqa: BLE001
                self._tflite = None
                self._logger.emit("warning", "vision.mouth_activity", "tflite_unavailable", {"error": str(exc)})
                if backend == "tflite":
                    return

        if backend in {"auto", "facemesh"} and use_facemesh:
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

    def _estimate_visual_state(
        self,
        track_key: str,
        frame: np.ndarray,
        bbox: BBox,
        width: int,
        height: int,
        mesh_faces: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        motion_activity = float(self._mouth.compute(track_key, frame, bbox))
        near_edge = _near_frame_edge(bbox, width, height, self._mesh_edge_margin)
        edge_quality = 0.6 if near_edge else 1.0
        if self._tflite is not None and not near_edge:
            try:
                tflite_state = self._tflite.estimate_state(frame, bbox)
            except Exception as exc:  # noqa: BLE001
                self._logger.emit("warning", "vision.mouth_activity", "tflite_failed", {"error": str(exc)})
                tflite_state = None
            if tflite_state is not None:
                return _visual_state_from_features(
                    mouth_activity=float(tflite_state.get("activity", 0.0)),
                    motion_activity=motion_activity,
                    landmark_presence=float(tflite_state.get("presence", 0.0)),
                    edge_quality=edge_quality,
                    motion_weight=self._visual_motion_weight,
                    quality_floor=self._visual_quality_floor,
                    backend="tflite",
                )
        mesh_match = _match_mesh_face(bbox, mesh_faces)
        if mesh_match is not None and not near_edge:
            return _visual_state_from_features(
                mouth_activity=float(mesh_match.get("activity", 0.0)),
                motion_activity=motion_activity,
                landmark_presence=float(mesh_match.get("presence", 1.0)),
                edge_quality=edge_quality,
                motion_weight=self._visual_motion_weight,
                quality_floor=self._visual_quality_floor,
                backend=str(mesh_match.get("backend", "facemesh")),
            )
        return _visual_state_from_features(
            mouth_activity=motion_activity,
            motion_activity=motion_activity,
            landmark_presence=0.0,
            edge_quality=edge_quality,
            motion_weight=1.0,
            quality_floor=self._visual_quality_floor,
            backend="diff",
        )

    def _load_yunet_detector(self):
        if not hasattr(cv2, "FaceDetectorYN"):
            return None
        try:
            model_path = _ensure_yunet_model(self._yunet_model_path)
            if hasattr(cv2.FaceDetectorYN, "create"):
                detector = cv2.FaceDetectorYN.create(
                    model_path,
                    "",
                    (self._detect_width or 320, self._detect_width or 320),
                    self._yunet_score_threshold,
                    self._yunet_nms_threshold,
                    self._yunet_top_k,
                )
            else:
                detector = cv2.FaceDetectorYN_create(
                    model_path,
                    "",
                    (self._detect_width or 320, self._detect_width or 320),
                    self._yunet_score_threshold,
                    self._yunet_nms_threshold,
                    self._yunet_top_k,
                )
        except Exception as exc:  # noqa: BLE001
            self._logger.emit("warning", "vision.face_track", "yunet_unavailable", {"camera_id": self._camera_id, "error": str(exc)})
            return None
        return detector

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

    def _detect_faces(self, frame_bgr) -> List[Tuple[BBox, float]]:
        if self._detector is None:
            return []
        height, width = frame_bgr.shape[:2]
        scale = 1.0
        resized = frame_bgr
        if self._detect_width > 0 and width > self._detect_width:
            scale = self._detect_width / float(width)
            resized = cv2.resize(frame_bgr, (self._detect_width, int(height * scale)), interpolation=cv2.INTER_AREA)
        min_size = max(20, int((self._min_area ** 0.5) * scale))
        if self._detector_kind == "yunet":
            detections = self._detect_faces_yunet(resized, scale)
        else:
            detections = self._detect_faces_haar(resized, scale, min_size)
        return [item for item in detections if _bbox_area(item[0]) >= self._min_area]

    def _detect_faces_haar(self, resized_frame, scale: float, min_size: int) -> List[Tuple[BBox, float]]:
        if self._detector is None:
            return []
        gray_frame = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2GRAY)
        detections = self._detector.detectMultiScale(
            gray_frame,
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
            results.append((bbox, 1.0))
        return results

    def _detect_faces_yunet(self, resized_frame, scale: float) -> List[Tuple[BBox, float]]:
        if self._detector is None:
            return []
        height, width = resized_frame.shape[:2]
        input_size = (int(width), int(height))
        if self._yunet_input_size != input_size:
            self._detector.setInputSize(input_size)
            self._yunet_input_size = input_size
        _retval, faces = self._detector.detect(resized_frame)
        results: List[Tuple[BBox, float]] = []
        if faces is None:
            return results
        for row in np.asarray(faces):
            x, y, w, h = row[:4]
            score = float(row[-1]) if row.shape[0] > 14 else self._yunet_score_threshold
            if scale != 1.0:
                x = float(x / scale)
                y = float(y / scale)
                w = float(w / scale)
                h = float(h / scale)
            bbox = (int(round(x)), int(round(y)), int(round(w)), int(round(h)))
            results.append((bbox, score))
        return results


def start_face_tracking(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    cameras = config.get("video", {}).get("cameras", [])
    q_calibration = bus.subscribe("vision.camera_calibration")
    queues: Dict[str, queue.Queue] = {}
    trackers: Dict[str, CameraTracker] = {}
    latest_tracks: Dict[str, List[Dict[str, Any]]] = {}
    for index, cam_cfg in enumerate(cameras):
        camera_id = cam_cfg.get("id", f"cam{index}")
        topic = f"vision.frames.{camera_id}"
        queues[camera_id] = bus.subscribe(topic)
        trackers[camera_id] = CameraTracker(camera_id, config, cam_cfg, logger)
        latest_tracks[camera_id] = []

    def _drain_latest(q: queue.Queue) -> Optional[Dict[str, Any]]:
        frame_msg: Optional[Dict[str, Any]] = None
        try:
            while True:
                frame_msg = q.get_nowait()
        except queue.Empty:
            pass
        return frame_msg

    def _run() -> None:
        idle_cycles = 0
        processed_cycles = 0
        next_stats_emit = time.time() + 1.0
        while not stop_event.is_set():
            calibration_msg = _drain_latest(q_calibration)
            if isinstance(calibration_msg, dict):
                raw_cameras = calibration_msg.get("cameras", [])
                applied: List[Dict[str, Any]] = []
                if isinstance(raw_cameras, list):
                    for item in raw_cameras:
                        if not isinstance(item, dict):
                            continue
                        camera_id = str(item.get("id", "") or "").strip()
                        tracker = trackers.get(camera_id)
                        if tracker is None:
                            continue
                        applied.append(tracker.apply_calibration(item))
                if applied:
                    logger.emit("info", "vision.face_track", "camera_calibration_applied", {"cameras": applied})
                    bus.publish("vision.face_tracks.debug", {"camera_calibration": applied})
            updated = False
            for camera_id, q in queues.items():
                frame_msg = _drain_latest(q)
                if frame_msg is None:
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
                processed_cycles += 1
            else:
                idle_cycles += 1
                time.sleep(0.003)

            now_s = time.time()
            if now_s >= next_stats_emit:
                bus.publish(
                    "runtime.worker_loop",
                    {
                        "t_ns": now_ns(),
                        "module": "vision.face_track",
                        "idle_cycles": int(idle_cycles),
                        "processed_cycles": int(processed_cycles),
                    },
                )
                next_stats_emit = now_s + 1.0

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


def _ensure_yunet_model(model_path: Optional[str]) -> str:
    if model_path:
        path = Path(model_path)
    else:
        cache_dir = Path.home() / ".cache" / "focusfield"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "face_detection_yunet_2023mar.onnx"
    if not path.exists():
        if not _runtime_downloads_allowed():
            raise RuntimeError(
                f"runtime model downloads disabled and YuNet model is missing: {path}. "
                "Set vision.face.yunet_model_path to a bundled local asset."
            )
        url = (
            "https://github.com/opencv/opencv_zoo/raw/main/models/"
            "face_detection_yunet/face_detection_yunet_2023mar.onnx"
        )
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to download YuNet model: {exc}") from exc
    return str(path)


def _visual_state_from_features(
    mouth_activity: float,
    motion_activity: float,
    landmark_presence: float,
    edge_quality: float,
    motion_weight: float,
    quality_floor: float,
    backend: str,
) -> Dict[str, Any]:
    mouth = float(np.clip(mouth_activity, 0.0, 1.0))
    motion = float(np.clip(motion_activity, 0.0, 1.0))
    presence = float(np.clip(landmark_presence, 0.0, 1.0))
    edge = float(np.clip(edge_quality, 0.0, 1.0))
    motion_w = float(np.clip(motion_weight, 0.0, 1.0))
    blended = ((1.0 - motion_w) * mouth) + (motion_w * motion)
    presence_quality = max(presence, 0.4) if backend == "diff" else presence
    quality = max(float(quality_floor), (0.7 * presence_quality) + (0.3 * edge))
    visual_prob = float(np.clip((0.78 * blended) + (0.22 * quality), 0.0, 1.0))
    return {
        "mouth_activity": mouth,
        "motion_activity": motion,
        "landmark_presence": presence,
        "visual_quality": float(np.clip(quality, 0.0, 1.0)),
        "visual_speaking_prob": visual_prob,
        "backend": backend,
    }


def _runtime_downloads_allowed() -> bool:
    raw = str(os.environ.get("FOCUSFIELD_ALLOW_RUNTIME_DOWNLOADS", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}

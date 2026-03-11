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

import json
import queue
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from focusfield.core.clock import now_ns
from focusfield.vision.calibration.bearing import bearing_from_bbox
from focusfield.vision.mouth.mouth_activity import FaceMeshMouthEstimator, MouthActivityEstimator
from focusfield.vision.mouth.thresholds import SpeakingHysteresis
from focusfield.vision.tracking.track_smoothing import TrackSmoother


BBox = Tuple[int, int, int, int]
Detection = Tuple[BBox, float]
DEFAULT_YUNET_MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
)


class _DetectorBase:
    def detect(self, frame_bgr: np.ndarray, gray_frame: np.ndarray) -> List[Detection]:
        raise NotImplementedError

    @property
    def backend(self) -> str:
        raise NotImplementedError


class _NullDetector(_DetectorBase):
    @property
    def backend(self) -> str:
        return "none"

    def detect(self, frame_bgr: np.ndarray, gray_frame: np.ndarray) -> List[Detection]:
        return []


class _HaarDetector(_DetectorBase):
    def __init__(
        self,
        *,
        camera_id: str,
        logger: Any,
        min_area: int,
        min_neighbors: int,
        scale_factor: float,
        detect_width: int,
    ) -> None:
        self._camera_id = camera_id
        self._logger = logger
        self._min_area = min_area
        self._min_neighbors = min_neighbors
        self._scale_factor = scale_factor
        self._detect_width = detect_width
        self._cascade = self._load_face_cascade()

    @property
    def backend(self) -> str:
        return "haar"

    def detect(self, frame_bgr: np.ndarray, gray_frame: np.ndarray) -> List[Detection]:
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
        results: List[Detection] = []
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

    def _load_face_cascade(self) -> Optional[cv2.CascadeClassifier]:
        candidates: List[Path] = []

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


class _YuNetDetector(_DetectorBase):
    def __init__(
        self,
        *,
        model_path: str,
        min_area: int,
        min_confidence: float,
        score_threshold: float,
        nms_threshold: float,
        top_k: int,
        input_width: int,
        input_height: int,
    ) -> None:
        if not hasattr(cv2, "FaceDetectorYN_create"):
            raise RuntimeError("FaceDetectorYN_create not available in this OpenCV build")
        self._model_path = model_path
        self._min_area = int(max(1, min_area))
        self._min_confidence = float(max(0.0, min(1.0, min_confidence)))
        self._score_threshold = float(max(0.0, min(1.0, score_threshold)))
        self._nms_threshold = float(max(0.0, min(1.0, nms_threshold)))
        self._top_k = int(max(1, top_k))
        self._input_width = int(max(16, input_width))
        self._input_height = int(max(16, input_height))
        self._detector: Any = None
        self._last_input_size: Optional[Tuple[int, int]] = None

    @property
    def backend(self) -> str:
        return "yunet"

    def _get_detector(self) -> Any:
        if self._detector is None:
            self._detector = cv2.FaceDetectorYN_create(
                self._model_path,
                "",
                (self._input_width, self._input_height),
                self._score_threshold,
                self._nms_threshold,
                self._top_k,
            )
        return self._detector

    def detect(self, frame_bgr: np.ndarray, gray_frame: np.ndarray) -> List[Detection]:
        height, width = frame_bgr.shape[:2]
        if width <= 0 or height <= 0:
            return []
        detector = self._get_detector()
        current_size = (int(width), int(height))
        if self._last_input_size != current_size:
            detector.setInputSize(current_size)
            self._last_input_size = current_size
        _, faces = detector.detect(frame_bgr)
        if faces is None:
            return []
        results: List[Detection] = []
        for row in faces:
            if row is None or len(row) < 15:
                continue
            x, y, w, h = row[0:4]
            score = float(row[14])
            if score < self._min_confidence:
                continue
            bbox = (
                max(0, int(round(x))),
                max(0, int(round(y))),
                max(0, int(round(w))),
                max(0, int(round(h))),
            )
            if _bbox_area(bbox) < self._min_area:
                continue
            results.append((bbox, score))
        return results


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
        self._detect_every_n = max(1, int(face_cfg.get("detect_every_n", 1)))
        self._full_frame_every_n = max(1, int(face_cfg.get("full_frame_every_n", 4)))
        self._roi_margin_ratio = float(face_cfg.get("roi_margin_ratio", 0.2))
        self._max_rois_per_frame = max(1, int(face_cfg.get("max_rois_per_frame", 4)))
        self._min_age_frames = int(track_cfg.get("min_age_frames", 2))
        self._min_confidence = float(face_cfg.get("min_confidence", 0.6))
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
        pose_cfg = face_cfg.get("pose", {}) if isinstance(face_cfg, dict) else {}
        if not isinstance(pose_cfg, dict):
            pose_cfg = {}
        self._pose_disconnect_angle = float(pose_cfg.get("disconnect_angle_deg", 55.0) or 55.0)
        self._pose_reconnect_angle = float(pose_cfg.get("reconnect_angle_deg", 42.0) or 42.0)
        if self._pose_reconnect_angle >= self._pose_disconnect_angle:
            self._pose_reconnect_angle = max(0.0, self._pose_disconnect_angle - 5.0)
        self._pose_off_angle_drop_ms = float(pose_cfg.get("off_angle_drop_ms", 1200.0) or 1200.0)
        self._pose_decay_alpha = max(0.0, min(1.0, float(pose_cfg.get("decay_alpha", 0.35) or 0.35)))
        self._off_angle_start_ns: Dict[int, int] = {}
        self._facing_score: Dict[int, float] = {}
        self._camera_cfg = camera_cfg
        self._frame_count = 0
        self._last_detections: List[Detection] = []
        self._last_detect_ms: float = 0.0
        self._last_roi_mode = "full-frame"
        self._detect_times: List[float] = []
        self._detect_fps: float = 0.0
        self._bearing_model = str(camera_cfg.get("bearing_model", "linear")).lower()
        self._bearing_lut = _load_bearing_lut(camera_cfg.get("bearing_lut_path"), camera_id, logger)
        self._preprocess_cfg = face_cfg.get("preprocess", {}) if isinstance(face_cfg, dict) else {}

        detector, active_backend, degraded_reason = _build_detector(camera_id, face_cfg, logger, self._min_area)
        self._detector = detector
        self._detector_backend = active_backend
        self._detector_degraded_reason = degraded_reason

    def process_frame(self, frame_msg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        frame = frame_msg["data"]
        height, width = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        detect_gray = self._preprocess_gray(gray)
        detect_bgr = frame
        if isinstance(self._preprocess_cfg, dict) and bool(self._preprocess_cfg.get("enabled", False)):
            detect_bgr = cv2.cvtColor(detect_gray, cv2.COLOR_GRAY2BGR)
        self._frame_count += 1

        run_detection = (self._frame_count % self._detect_every_n == 0) or not self._last_detections
        mode = "cache"
        detections: List[Detection] = self._last_detections

        if run_detection:
            detect_started = time.perf_counter()
            should_full = (self._frame_count % self._full_frame_every_n == 0) or not self._last_detections
            if should_full:
                detections = self._detect_full(detect_bgr, detect_gray)
                mode = "full-frame"
            else:
                detections = self._detect_rois(detect_bgr, detect_gray)
                mode = "roi"
                if not detections:
                    detections = self._detect_full(detect_bgr, detect_gray)
                    mode = "full-recover"
            self._last_detections = detections
            self._last_detect_ms = (time.perf_counter() - detect_started) * 1000.0
            self._record_detect_timestamp()
        self._last_roi_mode = mode

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
                self._off_angle_start_ns.pop(track_id, None)
                self._facing_score.pop(track_id, None)

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
            frame_t_ns = int(frame_msg.get("t_ns", now_ns()) or now_ns())
            bbox_face_angle_deg = _face_angle_proxy_deg(
                bbox=bbox,
                frame_width=width,
                hfov_deg=float(self._camera_cfg.get("hfov_deg", 90.0)),
            )
            mesh_match = _match_mesh_face(bbox, mesh_faces)
            face_angle_deg = bbox_face_angle_deg
            if mesh_match is not None:
                try:
                    mesh_face_angle = float(mesh_match.get("face_yaw_deg", bbox_face_angle_deg))
                    face_angle_deg = 0.7 * mesh_face_angle + 0.3 * bbox_face_angle_deg
                except Exception:  # noqa: BLE001
                    face_angle_deg = bbox_face_angle_deg
            facing_score = self._update_facing_score(track.track_id, face_angle_deg, frame_t_ns)
            if facing_score <= 0.01:
                continue
            if mesh_match is not None and not _near_frame_edge(bbox, width, height, self._mesh_edge_margin):
                mesh_activity = float(mesh_match.get("activity", 0.0))
                mesh_activity = max(0.0, min(1.0, mesh_activity))
                mouth_motion_score = self._mouth.smooth(track_key, mesh_activity)
                mouth_aperture_score = float(
                    max(
                        0.0,
                        min(
                            1.0,
                            float(mesh_match.get("mouth_aperture_score", mesh_activity)),
                        ),
                    )
                )
            else:
                mouth_motion_score = self._mouth.compute(track_key, frame, bbox)
                mouth_aperture_score = max(0.0, min(1.0, mouth_motion_score * 0.75))
            mouth_activity = max(0.0, min(1.0, 0.65 * mouth_motion_score + 0.35 * mouth_aperture_score))
            mouth_motion_score *= facing_score
            mouth_aperture_score *= facing_score
            mouth_activity *= facing_score
            speaking_tracker = self._speaking.get(track.track_id)
            if speaking_tracker is None:
                speaking_tracker = SpeakingHysteresis(
                    speak_on_threshold=self._speak_on,
                    speak_off_threshold=self._speak_off,
                    min_on_frames=self._speak_on_frames,
                    min_off_frames=self._speak_off_frames,
                )
                self._speaking[track.track_id] = speaking_tracker
            speaking = speaking_tracker.update(mouth_activity)
            motion_on = mouth_motion_score >= self._speak_on
            aperture_on = mouth_aperture_score >= max(0.25, self._speak_on * 0.6)
            if motion_on and aperture_on:
                speaking_evidence = "both"
            elif motion_on:
                speaking_evidence = "mouth_motion"
            elif aperture_on:
                speaking_evidence = "mouth_aperture"
            else:
                speaking_evidence = "none"
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
                    "confidence": float(track.confidence) * float(facing_score),
                    "bearing_deg": bearing,
                    "mouth_activity": mouth_activity,
                    "mouth_motion_score": mouth_motion_score,
                    "mouth_aperture_score": mouth_aperture_score,
                    "face_angle_deg": float(face_angle_deg),
                    "facing_score": float(facing_score),
                    "speaking_evidence": speaking_evidence,
                    "speaking": speaking,
                    "camera_id": self._camera_id,
                }
            )

        debug = {
            "camera_id": self._camera_id,
            "detector_backend": self._detector_backend,
            "detect_fps": float(self._detect_fps),
            "face_count": int(len(output_tracks)),
            "last_detect_ms": float(self._last_detect_ms),
            "roi_mode": self._last_roi_mode,
            "detector_degraded": bool(self._detector_degraded_reason),
            "detector_degraded_reason": str(self._detector_degraded_reason or ""),
        }
        return output_tracks, debug

    def _update_facing_score(self, track_id: int, face_angle_deg: float, t_ns: int) -> float:
        angle_abs = abs(float(face_angle_deg))
        if angle_abs <= self._pose_reconnect_angle:
            target = 1.0
            self._off_angle_start_ns.pop(track_id, None)
        elif angle_abs >= self._pose_disconnect_angle:
            target = 0.0
            self._off_angle_start_ns.setdefault(track_id, int(t_ns))
        else:
            ratio = (angle_abs - self._pose_reconnect_angle) / max(
                1e-6, self._pose_disconnect_angle - self._pose_reconnect_angle
            )
            target = 1.0 - max(0.0, min(1.0, ratio))
        prev = self._facing_score.get(track_id, target)
        score = (1.0 - self._pose_decay_alpha) * prev + self._pose_decay_alpha * target
        off_start = self._off_angle_start_ns.get(track_id)
        if off_start is not None:
            elapsed_ms = max(0.0, (int(t_ns) - int(off_start)) / 1_000_000.0)
            if elapsed_ms >= self._pose_off_angle_drop_ms:
                score = 0.0
        if angle_abs <= self._pose_reconnect_angle:
            score = max(score, 0.85)
        score = max(0.0, min(1.0, score))
        self._facing_score[track_id] = score
        return score

    def _record_detect_timestamp(self) -> None:
        ts = time.perf_counter()
        self._detect_times.append(ts)
        if len(self._detect_times) > 30:
            self._detect_times = self._detect_times[-30:]
        if len(self._detect_times) >= 2:
            duration = self._detect_times[-1] - self._detect_times[0]
            if duration > 0:
                self._detect_fps = float((len(self._detect_times) - 1) / duration)

    def _preprocess_gray(self, gray: np.ndarray) -> np.ndarray:
        cfg = self._preprocess_cfg if isinstance(self._preprocess_cfg, dict) else {}
        if not bool(cfg.get("enabled", False)):
            return gray
        output = gray
        clahe_tile = int(cfg.get("clahe_tile", 8) or 8)
        clahe_tile = max(1, clahe_tile)
        clahe_clip = float(cfg.get("clahe_clip_limit", 2.0) or 2.0)
        clahe_clip = max(0.01, clahe_clip)
        clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(clahe_tile, clahe_tile))
        output = clahe.apply(output)
        gamma = float(cfg.get("gamma", 1.0) or 1.0)
        if abs(gamma - 1.0) > 1e-3:
            output = _apply_gamma(output, gamma)
        blur_kernel = int(cfg.get("blur_kernel", 0) or 0)
        if blur_kernel > 0:
            if blur_kernel % 2 == 0:
                blur_kernel += 1
            output = cv2.GaussianBlur(output, (blur_kernel, blur_kernel), 0)
        return output

    def _detect_full(self, frame_bgr: np.ndarray, gray_frame: np.ndarray) -> List[Detection]:
        detections = self._detector.detect(frame_bgr, gray_frame)
        return _dedupe_detections(detections)

    def _detect_rois(self, frame_bgr: np.ndarray, gray_frame: np.ndarray) -> List[Detection]:
        height, width = gray_frame.shape[:2]
        rois = _build_rois(
            [bbox for bbox, _ in self._last_detections],
            width,
            height,
            margin_ratio=self._roi_margin_ratio,
            max_count=self._max_rois_per_frame,
        )
        if not rois:
            return []
        detections: List[Detection] = []
        for roi in rois:
            x, y, w, h = roi
            if w <= 1 or h <= 1:
                continue
            crop_bgr = frame_bgr[y : y + h, x : x + w]
            crop_gray = gray_frame[y : y + h, x : x + w]
            for bbox, score in self._detector.detect(crop_bgr, crop_gray):
                bx, by, bw, bh = bbox
                abs_bbox = (int(bx + x), int(by + y), int(bw), int(bh))
                if _bbox_area(abs_bbox) < self._min_area:
                    continue
                detections.append((abs_bbox, score))
        return _dedupe_detections(detections)


def start_face_tracking(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> List[threading.Thread]:
    cameras = config.get("video", {}).get("cameras", [])
    queues: Dict[str, queue.Queue] = {}
    trackers: Dict[str, CameraTracker] = {}
    latest_tracks: Dict[str, List[Dict[str, Any]]] = {}
    latest_debug: Dict[str, Dict[str, Any]] = {}
    latest_lock = threading.Lock()
    update_event = threading.Event()
    camera_ids: List[str] = []

    for index, cam_cfg in enumerate(cameras):
        camera_id = cam_cfg.get("id", f"cam{index}")
        topic = f"vision.frames.{camera_id}"
        queues[camera_id] = bus.subscribe(topic)
        trackers[camera_id] = CameraTracker(camera_id, config, cam_cfg, logger)
        latest_tracks[camera_id] = []
        latest_debug[camera_id] = {}
        camera_ids.append(str(camera_id))

    def _camera_worker(camera_id: str) -> None:
        q = queues[camera_id]
        tracker = trackers[camera_id]
        idle_cycles = 0
        processed_cycles = 0
        next_stats_emit = time.time() + 1.0
        while not stop_event.is_set():
            try:
                frame_msg = q.get(timeout=0.05)
            except queue.Empty:
                idle_cycles += 1
                now_s = time.time()
                if now_s >= next_stats_emit:
                    bus.publish(
                        "runtime.worker_loop",
                        {
                            "t_ns": now_ns(),
                            "module": f"vision.face_track.{camera_id}",
                            "idle_cycles": int(idle_cycles),
                            "processed_cycles": int(processed_cycles),
                        },
                    )
                    next_stats_emit = now_s + 1.0
                continue
            try:
                while True:
                    frame_msg = q.get_nowait()
            except queue.Empty:
                pass

            try:
                tracks, debug = tracker.process_frame(frame_msg)
            except Exception as exc:  # noqa: BLE001
                logger.emit("error", "vision.face_track", "detector_failed", {"camera_id": camera_id, "error": str(exc)})
                continue

            bus.publish(f"vision.face_tracks.{camera_id}", tracks)
            with latest_lock:
                latest_tracks[camera_id] = tracks
                latest_debug[camera_id] = debug
            update_event.set()
            processed_cycles += 1

            now_s = time.time()
            if now_s >= next_stats_emit:
                bus.publish(
                    "runtime.worker_loop",
                    {
                        "t_ns": now_ns(),
                        "module": f"vision.face_track.{camera_id}",
                        "idle_cycles": int(idle_cycles),
                        "processed_cycles": int(processed_cycles),
                    },
                )
                next_stats_emit = now_s + 1.0

    threads: List[threading.Thread] = []
    for camera_id in camera_ids:
        thread = threading.Thread(target=_camera_worker, name=f"face-track-{camera_id}", args=(camera_id,), daemon=True)
        thread.start()
        threads.append(thread)

    def _run() -> None:
        idle_cycles = 0
        processed_cycles = 0
        next_stats_emit = time.time() + 1.0
        next_publish = time.time()
        seq = 0
        while not stop_event.is_set():
            got_update = update_event.wait(timeout=0.05)
            if got_update:
                update_event.clear()
            now_s = time.time()
            if not got_update and now_s < next_publish:
                idle_cycles += 1
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
                continue
            next_publish = now_s + 0.25
            with latest_lock:
                tracks_snapshot = {camera_id: list(tracks) for camera_id, tracks in latest_tracks.items()}
                debug_snapshot = {camera_id: dict(debug) for camera_id, debug in latest_debug.items()}
            merged: List[Dict[str, Any]] = []
            for tracks in tracks_snapshot.values():
                merged.extend(tracks)
            bus.publish("vision.face_tracks", merged)
            seq += 1
            bus.publish("vision.face_tracks.debug", _build_debug_summary(debug_snapshot, camera_ids, seq))
            processed_cycles += 1
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

    aggregator_thread = threading.Thread(target=_run, name="face-track", daemon=True)
    aggregator_thread.start()
    threads.append(aggregator_thread)
    return threads


def _build_debug_summary(debug_snapshot: Dict[str, Dict[str, Any]], camera_ids: List[str], seq: int) -> Dict[str, Any]:
    detect_fps_by_camera: Dict[str, float] = {}
    face_count_by_camera: Dict[str, int] = {}
    last_detect_ms_by_camera: Dict[str, float] = {}
    roi_mode_by_camera: Dict[str, str] = {}
    degraded_reasons: Dict[str, str] = {}
    backends: List[str] = []

    for camera_id in camera_ids:
        camera_debug = debug_snapshot.get(camera_id, {})
        backend = str(camera_debug.get("detector_backend", "") or "")
        if backend:
            backends.append(backend)
        detect_fps_by_camera[camera_id] = float(camera_debug.get("detect_fps", 0.0) or 0.0)
        face_count_by_camera[camera_id] = int(camera_debug.get("face_count", 0) or 0)
        last_detect_ms_by_camera[camera_id] = float(camera_debug.get("last_detect_ms", 0.0) or 0.0)
        roi_mode_by_camera[camera_id] = str(camera_debug.get("roi_mode", "unknown") or "unknown")
        reason = str(camera_debug.get("detector_degraded_reason", "") or "")
        if reason:
            degraded_reasons[camera_id] = reason

    active_backend = "mixed"
    unique_backends = sorted(set(backends))
    if len(unique_backends) == 1:
        active_backend = unique_backends[0]
    elif len(unique_backends) == 0:
        active_backend = "none"

    return {
        "t_ns": now_ns(),
        "seq": seq,
        "detector_backend": active_backend,
        "detect_fps_by_camera": detect_fps_by_camera,
        "face_count_by_camera": face_count_by_camera,
        "last_detect_ms_by_camera": last_detect_ms_by_camera,
        "roi_mode_by_camera": roi_mode_by_camera,
        "detector_degraded": {
            "active": bool(degraded_reasons),
            "reasons_by_camera": degraded_reasons,
        },
    }


def _build_detector(camera_id: str, face_cfg: Dict[str, Any], logger: Any, min_area: int) -> Tuple[_DetectorBase, str, str]:
    requested_backend = str(face_cfg.get("detector_backend", "haar") or "haar").strip().lower()
    resolved_backend = "yunet" if requested_backend == "blazeface" else requested_backend
    alias_reason = "blazeface_alias_to_yunet" if requested_backend == "blazeface" else ""
    degraded_reason = alias_reason
    min_neighbors = int(face_cfg.get("min_neighbors", 4))
    scale_factor = float(face_cfg.get("scale_factor", 1.1))
    detect_width = int(face_cfg.get("detect_width", 360))
    min_confidence = float(face_cfg.get("min_confidence", 0.6))

    if resolved_backend == "yunet":
        yunet_cfg = face_cfg.get("yunet", {})
        if not isinstance(yunet_cfg, dict):
            yunet_cfg = {}
        model_path, model_source, model_error = _resolve_yunet_model(yunet_cfg)
        if model_path is not None:
            try:
                detector = _YuNetDetector(
                    model_path=model_path,
                    min_area=min_area,
                    min_confidence=min_confidence,
                    score_threshold=float(yunet_cfg.get("score_threshold", 0.75)),
                    nms_threshold=float(yunet_cfg.get("nms_threshold", 0.3)),
                    top_k=int(yunet_cfg.get("top_k", 5000)),
                    input_width=int(yunet_cfg.get("input_width", 320)),
                    input_height=int(yunet_cfg.get("input_height", 320)),
                )
                logger.emit(
                    "info",
                    "vision.face_track",
                    "detector_backend_active",
                    {
                        "camera_id": camera_id,
                        "requested_backend": requested_backend,
                        "active_backend": detector.backend,
                        "model_path": model_path,
                        "model_source": model_source,
                        "degraded_reason": alias_reason,
                    },
                )
                return detector, detector.backend, alias_reason
            except Exception as exc:  # noqa: BLE001
                model_error = f"yunet_init_failed:{exc}"

        degraded_reason = model_error or "yunet_unavailable"
        logger.emit(
            "warning",
            "vision.face_track",
            "detector_backend_fallback",
            {
                "camera_id": camera_id,
                "requested_backend": requested_backend,
                "fallback_backend": "haar",
                "reason": degraded_reason,
            },
        )
    elif resolved_backend not in {"haar"}:
        logger.emit(
            "warning",
            "vision.face_track",
            "detector_backend_fallback",
            {
                "camera_id": camera_id,
                "requested_backend": requested_backend,
                "fallback_backend": "haar",
                "reason": "unsupported_backend",
            },
        )
        degraded_reason = "unsupported_backend"

    detector = _HaarDetector(
        camera_id=camera_id,
        logger=logger,
        min_area=min_area,
        min_neighbors=min_neighbors,
        scale_factor=scale_factor,
        detect_width=detect_width,
    )
    logger.emit(
        "info",
        "vision.face_track",
        "detector_backend_active",
        {
            "camera_id": camera_id,
            "requested_backend": requested_backend,
            "active_backend": detector.backend,
            "degraded_reason": degraded_reason,
        },
    )
    return detector, detector.backend, degraded_reason


def _resolve_yunet_model(yunet_cfg: Dict[str, Any]) -> Tuple[Optional[str], str, str]:
    configured_path = str(yunet_cfg.get("model_path", "") or "").strip()
    if configured_path:
        path = Path(configured_path).expanduser()
        if path.exists():
            return str(path), "config", ""
        return None, "config", f"model_path_missing:{path}"

    cache_dir = Path.home() / ".cache" / "focusfield" / "models"
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_path = cache_dir / "face_detection_yunet_2023mar.onnx"
    if model_path.exists():
        return str(model_path), "cache", ""

    auto_download = bool(yunet_cfg.get("auto_download", True))
    if not auto_download:
        return None, "cache", "auto_download_disabled"

    try:
        with urllib.request.urlopen(DEFAULT_YUNET_MODEL_URL, timeout=15) as response:
            model_bytes = response.read()
        if not model_bytes:
            return None, "download", "download_empty"
        model_path.write_bytes(model_bytes)
    except Exception as exc:  # noqa: BLE001
        return None, "download", f"download_failed:{exc}"

    return str(model_path), "download", ""


def _build_rois(
    boxes: List[BBox],
    frame_width: int,
    frame_height: int,
    *,
    margin_ratio: float,
    max_count: int,
) -> List[BBox]:
    rois: List[BBox] = []
    if frame_width <= 0 or frame_height <= 0:
        return rois
    safe_margin = float(max(0.0, min(1.0, margin_ratio)))
    for bbox in boxes[: max(1, max_count)]:
        x, y, w, h = bbox
        if w <= 1 or h <= 1:
            continue
        margin_w = int(w * safe_margin)
        margin_h = int(h * safe_margin)
        roi_x = max(0, x - margin_w)
        roi_y = max(0, y - margin_h)
        roi_w = min(frame_width - roi_x, w + margin_w * 2)
        roi_h = min(frame_height - roi_y, h + margin_h * 2)
        if roi_w <= 1 or roi_h <= 1:
            continue
        rois.append((roi_x, roi_y, roi_w, roi_h))
    return rois


def _apply_gamma(gray: np.ndarray, gamma: float) -> np.ndarray:
    gamma = max(0.05, min(5.0, float(gamma)))
    inv = 1.0 / gamma
    lut = np.array([((i / 255.0) ** inv) * 255 for i in range(256)], dtype=np.uint8)
    return cv2.LUT(gray, lut)


def _dedupe_detections(detections: List[Detection], iou_threshold: float = 0.45) -> List[Detection]:
    if not detections:
        return []
    ordered = sorted(detections, key=lambda item: float(item[1]), reverse=True)
    kept: List[Detection] = []
    for candidate in ordered:
        bbox, score = candidate
        if _bbox_area(bbox) <= 0:
            continue
        if any(_iou(bbox, existing_bbox) >= iou_threshold for existing_bbox, _ in kept):
            continue
        kept.append((bbox, float(score)))
    return kept


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


def _face_angle_proxy_deg(bbox: BBox, frame_width: int, hfov_deg: float) -> float:
    if frame_width <= 1:
        return 0.0
    x, _, w, _ = bbox
    center_x = float(x) + 0.5 * float(w)
    mid = float(frame_width) * 0.5
    normalized = (center_x - mid) / max(1.0, mid)
    proxy = normalized * (float(hfov_deg) * 0.45)
    return float(max(-80.0, min(80.0, proxy)))


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

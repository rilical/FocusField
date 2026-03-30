"""
CONTRACT: inline (source: src/focusfield/vision/mouth/mouth_activity.md)
ROLE: Mouth activity estimation.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - vision.mouth.smoothing_alpha: EMA smoothing

PERF / TIMING:
  - per-frame

FAILURE MODES:
  - missing landmarks -> log landmarks_missing

LOG EVENTS:
  - module=vision.mouth_activity, event=landmarks_missing, payload keys=track_id
"""

from __future__ import annotations

import urllib.request
import zipfile
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

try:
    import mediapipe.tasks as mp_tasks
    from mediapipe.tasks.python.vision.core.image import Image, ImageFormat
except ImportError:  # pragma: no cover
    mp_tasks = None
    Image = None
    ImageFormat = None

try:
    from tflite_runtime.interpreter import Interpreter
except ImportError:  # pragma: no cover
    try:
        from ai_edge_litert.interpreter import Interpreter  # type: ignore
    except ImportError:  # pragma: no cover
        Interpreter = None


BBox = Tuple[int, int, int, int]


class MouthActivityEstimator:
    """Estimate mouth activity from frame differencing in mouth ROI."""

    def __init__(
        self,
        smoothing_alpha: float,
        min_activity: float,
        max_activity: float,
        diff_threshold: float = 12.0,
    ) -> None:
        self._alpha = smoothing_alpha
        self._min_activity = min_activity
        self._max_activity = max_activity
        self._diff_threshold = diff_threshold
        self._prev_roi: Dict[str, np.ndarray] = {}
        self._smoothed: Dict[str, float] = {}

    def compute(self, track_id: str, frame_bgr: np.ndarray, bbox: BBox) -> float:
        roi = _extract_mouth_roi(frame_bgr, bbox)
        if roi is None:
            return 0.0
        roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        roi_gray = cv2.GaussianBlur(roi_gray, (3, 3), 0)
        prev = self._prev_roi.get(track_id)
        if prev is None or prev.shape != roi_gray.shape:
            raw = 0.0
        else:
            diff = cv2.absdiff(roi_gray, prev)
            if self._diff_threshold > 0:
                diff = np.where(diff >= self._diff_threshold, diff, 0)
            raw = float(np.mean(diff)) / 255.0
        self._prev_roi[track_id] = roi_gray
        scaled = _scale(raw, self._min_activity, self._max_activity)
        return self.smooth(track_id, scaled)

    def smooth(self, track_id: str, value: float) -> float:
        previous = self._smoothed.get(track_id, 0.0)
        smoothed = self._alpha * value + (1.0 - self._alpha) * previous
        self._smoothed[track_id] = smoothed
        return smoothed

    def drop(self, track_id: str) -> None:
        self._prev_roi.pop(track_id, None)
        self._smoothed.pop(track_id, None)


class FaceMeshMouthEstimator:
    """MediaPipe Tasks FaceLandmarker-based mouth activity estimator."""

    def __init__(
        self,
        max_faces: int = 5,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        min_activity: float = 0.02,
        max_activity: float = 0.25,
        model_path: Optional[str] = None,
    ) -> None:
        if mp_tasks is None or Image is None or ImageFormat is None:
            raise RuntimeError("mediapipe tasks API is not available")
        model_path = _ensure_task_model_path(model_path)
        base_options = mp_tasks.BaseOptions(model_asset_path=model_path)
        options = mp_tasks.vision.FaceLandmarkerOptions(
            base_options=base_options,
            num_faces=max_faces,
            min_face_detection_confidence=min_detection_confidence,
            min_face_presence_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
        )
        self._landmarker = mp_tasks.vision.FaceLandmarker.create_from_options(options)
        self._min_activity = min_activity
        self._max_activity = max_activity

    def estimate(self, frame_bgr: np.ndarray) -> List[Dict[str, Any]]:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = Image(image_format=ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect(mp_image)
        if not result.face_landmarks:
            return []
        height, width = frame_bgr.shape[:2]
        outputs: List[Dict[str, Any]] = []
        for face_landmarks in result.face_landmarks:
            pts = [(int(lm.x * width), int(lm.y * height)) for lm in face_landmarks]
            x_coords = [p[0] for p in pts]
            y_coords = [p[1] for p in pts]
            x1, x2 = max(0, min(x_coords)), min(width - 1, max(x_coords))
            y1, y2 = max(0, min(y_coords)), min(height - 1, max(y_coords))
            bbox = (x1, y1, max(1, x2 - x1), max(1, y2 - y1))
            activity = _mouth_aspect_ratio(pts)
            scaled = _scale(activity, self._min_activity, self._max_activity)
            outputs.append({"bbox": bbox, "activity": scaled, "presence": 1.0, "backend": "facemesh"})
        return outputs


class TFLiteMouthEstimator:
    """Lightweight landmark-based mouth estimator using a standalone TFLite interpreter."""

    def __init__(
        self,
        min_activity: float = 0.02,
        max_activity: float = 0.25,
        model_path: Optional[str] = None,
        task_path: Optional[str] = None,
        model_member: str = "face_landmarks_detector.tflite",
        num_threads: int = 1,
        min_presence: float = 0.0,
        crop_scale: float = 1.45,
    ) -> None:
        if Interpreter is None:
            raise RuntimeError("tflite interpreter is not available")
        resolved_model = _ensure_tflite_model_path(model_path, task_path, model_member)
        self._interpreter = Interpreter(model_path=resolved_model, num_threads=max(1, int(num_threads)))
        self._interpreter.allocate_tensors()
        self._input_details = self._interpreter.get_input_details()
        self._output_details = self._interpreter.get_output_details()
        if not self._input_details:
            raise RuntimeError("tflite landmark model has no inputs")
        input_shape = list(self._input_details[0].get("shape", []))
        if len(input_shape) < 4:
            raise RuntimeError(f"unsupported landmark model input shape: {input_shape}")
        self._input_h = int(input_shape[1])
        self._input_w = int(input_shape[2])
        self._min_activity = min_activity
        self._max_activity = max_activity
        self._min_presence = min_presence
        self._crop_scale = crop_scale
        self._landmarks_output_index = _select_landmark_output(self._output_details)
        self._presence_output_index = _select_presence_output(self._output_details)
        if self._landmarks_output_index is None:
            raise RuntimeError("tflite landmark model output layout is unsupported")

    def estimate_state(self, frame_bgr: np.ndarray, bbox: BBox) -> Optional[Dict[str, float]]:
        crop = _extract_face_crop(frame_bgr, bbox, self._crop_scale)
        if crop is None:
            return None
        face_crop = crop["image"]
        rgb = cv2.cvtColor(face_crop, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (self._input_w, self._input_h), interpolation=cv2.INTER_LINEAR)

        input_detail = self._input_details[0]
        tensor = _prepare_input_tensor(rgb, input_detail)
        self._interpreter.set_tensor(int(input_detail["index"]), tensor)
        self._interpreter.invoke()

        landmarks_raw = _read_output_tensor(self._interpreter, self._output_details[self._landmarks_output_index])
        if landmarks_raw is None:
            return None
        presence = 1.0
        if self._presence_output_index is not None:
            presence_raw = _read_output_tensor(self._interpreter, self._output_details[self._presence_output_index])
            if presence_raw is not None and presence_raw.size:
                presence = float(np.ravel(presence_raw)[0])
        if presence < self._min_presence:
            return None

        points = _landmarks_to_points(
            landmarks_raw,
            crop_width=int(crop["w"]),
            crop_height=int(crop["h"]),
            input_width=self._input_w,
            input_height=self._input_h,
        )
        activity = _mouth_aspect_ratio(points)
        return {
            "activity": _scale(activity, self._min_activity, self._max_activity),
            "presence": float(np.clip(presence, 0.0, 1.0)),
            "quality": float(np.clip(presence, 0.0, 1.0)),
        }

    def estimate_activity(self, frame_bgr: np.ndarray, bbox: BBox) -> Optional[float]:
        state = self.estimate_state(frame_bgr, bbox)
        if state is None:
            return None
        return float(state.get("activity", 0.0))


def _mouth_aspect_ratio(points: List[Tuple[int, int]]) -> float:
    if len(points) < 292:
        return 0.0
    upper = points[13]
    lower = points[14]
    left = points[61]
    right = points[291]
    vertical = _distance(upper, lower)
    horizontal = _distance(left, right)
    if horizontal <= 1e-6:
        return 0.0
    return vertical / horizontal


def _extract_mouth_roi(frame: np.ndarray, bbox: BBox) -> Optional[np.ndarray]:
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        return None
    x1 = max(0, x + int(0.15 * w))
    x2 = max(0, x + int(0.85 * w))
    y1 = max(0, y + int(0.55 * h))
    y2 = max(0, y + int(0.9 * h))
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2]


def _extract_face_crop(frame: np.ndarray, bbox: BBox, scale: float) -> Optional[Dict[str, Any]]:
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        return None
    side = int(max(w, h) * max(1.0, scale))
    cx = x + w / 2.0
    cy = y + h / 2.0
    x1 = max(0, int(round(cx - side / 2.0)))
    y1 = max(0, int(round(cy - side / 2.0)))
    x2 = min(frame.shape[1], x1 + side)
    y2 = min(frame.shape[0], y1 + side)
    if x2 <= x1 or y2 <= y1:
        return None
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    return {"image": crop, "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def _scale(value: float, min_value: float, max_value: float) -> float:
    if max_value <= min_value:
        return 0.0
    scaled = (value - min_value) / (max_value - min_value)
    if scaled < 0.0:
        return 0.0
    if scaled > 1.0:
        return 1.0
    return scaled


def _distance(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    dx = float(a[0] - b[0])
    dy = float(a[1] - b[1])
    return float(np.hypot(dx, dy))


def _ensure_task_model_path(model_path: Optional[str]) -> str:
    if model_path:
        path = Path(model_path)
    else:
        cache_dir = Path.home() / ".cache" / "focusfield"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "face_landmarker.task"
    if not path.exists():
        if not _runtime_downloads_allowed():
            raise RuntimeError(
                f"runtime model downloads disabled and FaceLandmarker model is missing: {path}. "
                "Set vision.mouth.mesh_model_path to a bundled local asset."
            )
        url = (
            "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
            "face_landmarker/float16/latest/face_landmarker.task"
        )
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to download FaceLandmarker model: {exc}") from exc
    return str(path)


def _ensure_tflite_model_path(model_path: Optional[str], task_path: Optional[str], member_name: str) -> str:
    if model_path:
        path = Path(model_path).expanduser()
        if not path.exists():
            raise RuntimeError(f"tflite model does not exist: {path}")
        return str(path)
    task = Path(_ensure_task_model_path(task_path)).expanduser()
    cache_dir = task.parent
    target = cache_dir / member_name
    if target.exists():
        return str(target)
    if not zipfile.is_zipfile(task):
        raise RuntimeError(f"task archive is not a zip bundle: {task}")
    with zipfile.ZipFile(task) as zf:
        if member_name not in zf.namelist():
            raise RuntimeError(f"task bundle missing {member_name}")
        with zf.open(member_name) as src, open(target, "wb") as dst:
            dst.write(src.read())
    return str(target)


def _prepare_input_tensor(image_rgb: np.ndarray, detail: Dict[str, Any]) -> np.ndarray:
    dtype = np.dtype(detail.get("dtype", np.float32))
    x = np.asarray(image_rgb)
    if dtype == np.float32:
        x = x.astype(np.float32) / 255.0
    else:
        x = x.astype(np.float32)
        scale, zero_point = detail.get("quantization", (0.0, 0))
        if scale and float(scale) > 0:
            x = np.round(x / float(scale) + float(zero_point))
        x = np.clip(x, np.iinfo(dtype).min, np.iinfo(dtype).max).astype(dtype)
    return x[None, ...]


def _read_output_tensor(interpreter: Any, detail: Dict[str, Any]) -> Optional[np.ndarray]:
    try:
        out = interpreter.get_tensor(int(detail["index"]))
    except Exception:  # noqa: BLE001
        return None
    arr = np.asarray(out)
    scale, zero_point = detail.get("quantization", (0.0, 0))
    if scale and float(scale) > 0 and arr.dtype != np.float32:
        arr = (arr.astype(np.float32) - float(zero_point)) * float(scale)
    return arr.astype(np.float32, copy=False)


def _select_landmark_output(details: List[Dict[str, Any]]) -> Optional[int]:
    best_idx = None
    best_size = 0
    for idx, detail in enumerate(details):
        shape = detail.get("shape", [])
        size = int(np.prod(shape)) if shape is not None and len(shape) > 0 else 0
        if size >= 292 * 3 and size > best_size:
            best_idx = idx
            best_size = size
    return best_idx


def _select_presence_output(details: List[Dict[str, Any]]) -> Optional[int]:
    named: Optional[int] = None
    smallest: Optional[Tuple[int, int]] = None
    for idx, detail in enumerate(details):
        name = str(detail.get("name", "") or "").lower()
        shape = detail.get("shape", [])
        size = int(np.prod(shape)) if shape is not None and len(shape) > 0 else 0
        if size <= 4 and any(token in name for token in ("presence", "confidence", "score")):
            named = idx
            break
        if size <= 4 and (smallest is None or size < smallest[0]):
            smallest = (size, idx)
    if named is not None:
        return named
    return None if smallest is None else int(smallest[1])


def _landmarks_to_points(
    landmarks_raw: np.ndarray,
    crop_width: int,
    crop_height: int,
    input_width: int,
    input_height: int,
) -> List[Tuple[int, int]]:
    flat = np.asarray(landmarks_raw, dtype=np.float32).reshape(-1)
    count = flat.size // 3
    if count <= 0:
        return []
    coords = flat[: count * 3].reshape(count, 3)
    xs = coords[:, 0].copy()
    ys = coords[:, 1].copy()
    max_abs = float(max(np.max(np.abs(xs)), np.max(np.abs(ys)))) if coords.size else 0.0
    if max_abs > 2.0:
        xs /= max(1.0, float(input_width))
        ys /= max(1.0, float(input_height))
    xs = np.clip(xs, 0.0, 1.0)
    ys = np.clip(ys, 0.0, 1.0)
    points = [
        (int(round(float(x) * crop_width)), int(round(float(y) * crop_height)))
        for x, y in zip(xs.tolist(), ys.tolist())
    ]
    return points


def _runtime_downloads_allowed() -> bool:
    raw = str(os.environ.get("FOCUSFIELD_ALLOW_RUNTIME_DOWNLOADS", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}

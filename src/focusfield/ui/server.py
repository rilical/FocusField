"""
CONTRACT: inline (source: src/focusfield/ui/server.md)
ROLE: HTTP + WebSocket server for UI.

INPUTS:
  - Topic: ui.telemetry  Type: TelemetrySnapshot
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - ui.host: bind host
  - ui.port: bind port

PERF / TIMING:
  - serve at localhost; stable ws updates

FAILURE MODES:
  - bind failure -> log bind_failed -> exit

LOG EVENTS:
  - module=ui.server, event=bind_failed, payload keys=host, port, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/ui/server.md):
# UI server

- HTTP + WebSocket contract.
- Serve live and bench views.
- Stream telemetry at a stable update rate.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import cv2

from focusfield.ui.views.live import live_page


class UIState:
    """Thread-safe store for telemetry and frames."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._telemetry: Dict[str, Any] = {}
        self._frames: Dict[str, Any] = {}
        self._frame_jpegs: Dict[str, bytes] = {}
        self._frame_encode_ns: Dict[str, int] = {}

    def update_telemetry(self, telemetry: Dict[str, Any]) -> None:
        with self._lock:
            self._telemetry = telemetry

    def update_frame(self, camera_id: str, frame, jpeg_quality: int = 65, min_encode_period_s: float = 0.0) -> None:
        now_ns = time.time_ns()
        min_period_ns = int(max(0.0, float(min_encode_period_s)) * 1_000_000_000)
        should_encode = False
        with self._lock:
            self._frames[camera_id] = frame
            last_ns = int(self._frame_encode_ns.get(camera_id, 0) or 0)
            if not last_ns or now_ns - last_ns >= min_period_ns:
                should_encode = True
        if should_encode:
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
            if ok:
                jpeg_bytes = encoded.tobytes()
                with self._lock:
                    self._frame_jpegs[camera_id] = jpeg_bytes
                    self._frame_encode_ns[camera_id] = now_ns

    def get_telemetry(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._telemetry)

    def get_frame_jpeg(self, camera_id: str) -> Optional[bytes]:
        with self._lock:
            return self._frame_jpegs.get(camera_id)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def start_ui_server(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    ui_cfg = config.get("ui", {})
    if not isinstance(ui_cfg, dict):
        ui_cfg = {}
    host = ui_cfg.get("host", "0.0.0.0")
    port = int(ui_cfg.get("port", 8080))
    jpeg_quality = int(ui_cfg.get("frame_jpeg_quality", 65) or 65)
    jpeg_quality = max(1, min(100, jpeg_quality))
    frame_max_hz = float(ui_cfg.get("frame_max_hz", 6.0) or 6.0)
    frame_max_hz = max(0.1, frame_max_hz)
    frame_min_period_s = 1.0 / frame_max_hz
    cameras = [cam.get("id", f"cam{idx}") for idx, cam in enumerate(config.get("video", {}).get("cameras", []))]
    state = UIState()

    q_telemetry = bus.subscribe("ui.telemetry")
    frame_queues = {cam_id: bus.subscribe(f"vision.frames.{cam_id}") for cam_id in cameras}

    def _state_worker() -> None:
        def _drain_latest(q: queue.Queue) -> Optional[Dict[str, Any]]:
            item: Optional[Dict[str, Any]] = None
            try:
                while True:
                    item = q.get_nowait()
            except queue.Empty:
                pass
            return item

        while not stop_event.is_set():
            try:
                telemetry_msg = _drain_latest(q_telemetry)
                if telemetry_msg is not None:
                    state.update_telemetry(telemetry_msg)
            except Exception:
                pass
            for cam_id, q in frame_queues.items():
                frame_msg = _drain_latest(q)
                if frame_msg is None:
                    continue
                frame = frame_msg.get("data")
                if frame is not None:
                    state.update_frame(
                        cam_id,
                        frame,
                        jpeg_quality=jpeg_quality,
                        min_encode_period_s=frame_min_period_s,
                    )

    threading.Thread(target=_state_worker, name="ui-state", daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html(live_page())
                return
            if parsed.path.startswith("/frame/"):
                camera_id = parsed.path.split("/")[-1].replace(".jpg", "")
                jpeg = state.get_frame_jpeg(camera_id)
                if jpeg is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(jpeg)
                return
            if parsed.path == "/telemetry":
                payload = json.dumps(state.get_telemetry()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_html(self, html: str) -> None:
            payload = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)

    def _serve() -> None:
        try:
            server = ThreadedHTTPServer((host, port), Handler)
        except OSError as exc:
            logger.emit("error", "ui.server", "bind_failed", {"host": host, "port": port, "error": str(exc)})
            return
        server.timeout = 0.5
        logger.emit("info", "ui.server", "started", {"host": host, "port": port})
        while not stop_event.is_set():
            server.handle_request()
        server.server_close()

    thread = threading.Thread(target=_serve, name="ui-server", daemon=True)
    thread.start()
    return thread

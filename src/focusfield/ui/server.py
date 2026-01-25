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
import threading
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

    def update_telemetry(self, telemetry: Dict[str, Any]) -> None:
        with self._lock:
            self._telemetry = telemetry

    def update_frame(self, camera_id: str, frame) -> None:
        with self._lock:
            self._frames[camera_id] = frame

    def get_telemetry(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._telemetry)

    def get_frame(self, camera_id: str):
        with self._lock:
            return self._frames.get(camera_id)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def start_ui_server(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    host = config.get("ui", {}).get("host", "127.0.0.1")
    port = int(config.get("ui", {}).get("port", 8080))
    cameras = [cam.get("id", f"cam{idx}") for idx, cam in enumerate(config.get("video", {}).get("cameras", []))]
    state = UIState()

    q_telemetry = bus.subscribe("ui.telemetry")
    frame_queues = {cam_id: bus.subscribe(f"vision.frames.{cam_id}") for cam_id in cameras}

    def _state_worker() -> None:
        while not stop_event.is_set():
            try:
                telemetry = q_telemetry.get(timeout=0.1)
                state.update_telemetry(telemetry)
            except Exception:
                pass
            for cam_id, q in frame_queues.items():
                try:
                    frame_msg = q.get_nowait()
                except Exception:
                    continue
                frame = frame_msg.get("data")
                if frame is not None:
                    state.update_frame(cam_id, frame)

    threading.Thread(target=_state_worker, name="ui-state", daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html(live_page())
                return
            if parsed.path.startswith("/frame/"):
                camera_id = parsed.path.split("/")[-1].replace(".jpg", "")
                frame = state.get_frame(camera_id)
                if frame is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                ok, encoded = cv2.imencode(".jpg", frame)
                if not ok:
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded.tobytes())
                return
            if parsed.path == "/telemetry":
                payload = json.dumps(state.get_telemetry()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
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

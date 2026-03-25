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

import base64
import hashlib
import json
import queue
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

import cv2

from focusfield.vision.calibration.runtime_overlay import (
    apply_camera_calibration,
    load_camera_calibration,
    save_camera_calibration,
)
from focusfield.ui.views.live import live_page

_WS_MAGIC = b"258EAFA5-E914-47DA-95CA-5AB5DC69C85E"


def _ws_send_text(sock: socket.socket, text: str) -> None:
    """Send a WebSocket text frame."""
    data = text.encode("utf-8")
    frame = bytearray()
    frame.append(0x81)  # FIN + text opcode
    length = len(data)
    if length < 126:
        frame.append(length)
    elif length < 65536:
        frame.append(126)
        frame.extend(length.to_bytes(2, "big"))
    else:
        frame.append(127)
        frame.extend(length.to_bytes(8, "big"))
    frame.extend(data)
    sock.sendall(bytes(frame))


def _ws_send_pong(sock: socket.socket, payload: bytes) -> None:
    """Send a WebSocket pong frame."""
    frame = bytearray()
    frame.append(0x8A)  # FIN + pong opcode
    length = len(payload)
    if length < 126:
        frame.append(length)
    elif length < 65536:
        frame.append(126)
        frame.extend(length.to_bytes(2, "big"))
    else:
        frame.append(127)
        frame.extend(length.to_bytes(8, "big"))
    frame.extend(payload)
    sock.sendall(bytes(frame))


def _ws_send_close(sock: socket.socket, code: int = 1000) -> None:
    """Send a WebSocket close frame."""
    frame = bytearray()
    frame.append(0x88)  # FIN + close opcode
    payload = code.to_bytes(2, "big")
    frame.append(len(payload))
    frame.extend(payload)
    try:
        sock.sendall(bytes(frame))
    except OSError:
        pass


def _ws_read_frame(sock: socket.socket) -> Optional[tuple]:
    """Read a single WebSocket frame. Returns (opcode, payload) or None on error."""
    try:
        header = _recv_exact(sock, 2)
        if header is None:
            return None
    except OSError:
        return None

    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F

    if length == 126:
        raw = _recv_exact(sock, 2)
        if raw is None:
            return None
        length = struct.unpack("!H", raw)[0]
    elif length == 127:
        raw = _recv_exact(sock, 8)
        if raw is None:
            return None
        length = struct.unpack("!Q", raw)[0]

    mask_key = b""
    if masked:
        mask_key = _recv_exact(sock, 4)
        if mask_key is None:
            return None

    payload = _recv_exact(sock, length) if length > 0 else b""
    if payload is None:
        return None

    if masked and mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

    return (opcode, payload)


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """Read exactly n bytes from socket, or return None on disconnect."""
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _load_camera_calibration(config: Dict[str, Any]) -> Dict[str, Any]:
    calibration, _meta = load_camera_calibration(config)
    return calibration


def _save_camera_calibration(data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Write normalized camera calibration to the sidecar file."""
    return save_camera_calibration(data, config)


class UIState:
    """Thread-safe store for telemetry and frames."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._telemetry: Dict[str, Any] = {}
        self._frames: Dict[str, Any] = {}
        self._frame_jpegs: Dict[str, bytes] = {}
        self._frame_encode_ns: Dict[str, int] = {}
        self._ws_clients: Set[socket.socket] = set()
        self._ws_lock = threading.Lock()

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

    def add_ws_client(self, sock: socket.socket) -> None:
        with self._ws_lock:
            self._ws_clients.add(sock)

    def remove_ws_client(self, sock: socket.socket) -> None:
        with self._ws_lock:
            self._ws_clients.discard(sock)

    def broadcast_telemetry(self, telemetry: Dict[str, Any]) -> None:
        """Send telemetry JSON to all connected WebSocket clients."""
        with self._ws_lock:
            clients = list(self._ws_clients)
        if not clients:
            return
        try:
            text = json.dumps(telemetry)
        except (TypeError, ValueError):
            return
        dead: List[socket.socket] = []
        for sock in clients:
            try:
                _ws_send_text(sock, text)
            except OSError:
                dead.append(sock)
        if dead:
            with self._ws_lock:
                for sock in dead:
                    self._ws_clients.discard(sock)


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
                    state.broadcast_telemetry(telemetry_msg)
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

    def _ws_client_reader(sock: socket.socket) -> None:
        """Read frames from a WS client: handle ping, close, ignore others."""
        try:
            while not stop_event.is_set():
                result = _ws_read_frame(sock)
                if result is None:
                    break
                opcode, payload = result
                if opcode == 0x9:  # ping
                    try:
                        _ws_send_pong(sock, payload)
                    except OSError:
                        break
                elif opcode == 0x8:  # close
                    _ws_send_close(sock)
                    break
                # ignore text/binary/pong frames
        except Exception:
            pass
        finally:
            state.remove_ws_client(sock)
            try:
                sock.close()
            except OSError:
                pass

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)

            # WebSocket upgrade on /ws
            if parsed.path == "/ws":
                upgrade = self.headers.get("Upgrade", "").lower()
                if upgrade == "websocket":
                    self._handle_ws_upgrade()
                    return
                self.send_response(400)
                self.end_headers()
                return

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
            if parsed.path == "/api/camera-config":
                cal = _load_camera_calibration(config)
                payload = json.dumps(cal).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/camera-config":
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length > 0:
                    raw = self.rfile.read(content_length)
                else:
                    raw = b"{}"
                try:
                    body = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "invalid JSON"}).encode("utf-8"))
                    return
                normalized = _save_camera_calibration(body, config)
                apply_camera_calibration(config, normalized)
                _loaded, overlay_meta = load_camera_calibration(config)
                runtime_cfg = config.setdefault("runtime", {})
                if not isinstance(runtime_cfg, dict):
                    runtime_cfg = {}
                    config["runtime"] = runtime_cfg
                runtime_cfg["camera_calibration_overlay"] = {
                    **overlay_meta,
                    "applied_camera_ids": [
                        str(item.get("id", ""))
                        for item in normalized.get("cameras", [])
                        if isinstance(item, dict)
                    ],
                    "cameras": normalized.get("cameras", []),
                }
                bus.publish("vision.camera_calibration", normalized)
                resp = json.dumps({"status": "ok"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp)
                return
            self.send_response(404)
            self.end_headers()

        def do_OPTIONS(self) -> None:  # noqa: N802
            """Handle CORS preflight for POST endpoints."""
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _handle_ws_upgrade(self) -> None:
            """Perform WebSocket handshake and hand off to reader thread."""
            ws_key = self.headers.get("Sec-WebSocket-Key", "")
            if not ws_key:
                self.send_response(400)
                self.end_headers()
                return

            # Compute accept key
            accept_raw = hashlib.sha1(ws_key.encode("utf-8") + _WS_MAGIC).digest()
            accept_key = base64.b64encode(accept_raw).decode("utf-8")

            # Send 101 Switching Protocols
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept_key)
            self.end_headers()

            # Take ownership of the socket
            sock = self.request
            # Disable Nagle for low latency
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

            state.add_ws_client(sock)

            # Spawn reader thread to handle ping/pong/close from client
            reader_thread = threading.Thread(
                target=_ws_client_reader,
                args=(sock,),
                name="ws-reader",
                daemon=True,
            )
            reader_thread.start()

            # Block this handler thread so BaseHTTPRequestHandler doesn't close the socket
            reader_thread.join()

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

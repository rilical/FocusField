"""UMA-8 LED visualizer service.

By default this module is non-fatal and gracefully falls back to simulation.
When `uma8_leds.strict_transport=true`, transport failures are fatal.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from focusfield.core.clock import now_ns

_LOCK_STATES = {"LOCKED", "HOLD"}
_ACQUIRE_STATES = {"ACQUIRE", "HANDOFF"}
_NO_LOCK_STATES = {"NO_LOCK"}


@dataclass
class LedState:
    enabled: bool
    backend: str
    preferred_backend: str
    state: str
    source_bearing_deg: Optional[float]
    smoothed_bearing_deg: Optional[float]
    mapped_bearing_deg: Optional[float]
    sector: Optional[int]
    sectors: List[int]
    brightness: float
    rgb: Tuple[int, int, int]
    ring_size: int
    base_bearing_offset_deg: float
    transport_error: str
    device_count: Optional[int]


class Uma8LedTransport:
    """Abstract LED transport interface."""

    backend = "base"

    def open(self) -> bool:
        return True

    def send(self, led_state: LedState) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        return None


class NoopTransport(Uma8LedTransport):
    """No-op transport for disabled / unsupported hardware paths."""

    backend = "none"

    def send(self, led_state: LedState) -> bool:  # noqa: ARG002
        return True


class SimulateTransport(Uma8LedTransport):
    """In-memory transport for debug/telemetry verification."""

    backend = "simulate"

    def __init__(self) -> None:
        self.last_state: Optional[LedState] = None
        self.history: List[LedState] = []

    def send(self, led_state: LedState) -> bool:
        self.last_state = led_state
        self.history.append(led_state)
        # Prevent unbounded memory growth in long runs.
        if len(self.history) > 512:
            self.history = self.history[-256:]
        return True


class HidTransport(Uma8LedTransport):
    """Best-effort HID transport.

    UMA-8 LED protocol bytes are device/firmware specific and are not finalized
    in this repo yet. We still expose the transport boundary so the service can
    run with graceful fallback behavior.
    """

    backend = "hid"

    def __init__(self, vendor_id: int, product_id: int) -> None:
        self.vendor_id = int(vendor_id)
        self.product_id = int(product_id)
        self._hid_module: Any = None
        self._device: Any = None
        self.last_error: str = ""
        self.last_device_count: int = 0

    def open(self) -> bool:
        try:
            import hid  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"hid_import_failed:{exc}"
            return False

        self._hid_module = hid
        try:
            devices = hid.enumerate(self.vendor_id, self.product_id)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"hid_enumerate_failed:{exc}"
            return False

        self.last_device_count = len(devices)
        if not devices:
            self.last_error = f"hid_no_device vid=0x{self.vendor_id:04x} pid=0x{self.product_id:04x}"
            return False

        try:
            self._device = hid.device()
            path = devices[0].get("path")
            if path:
                self._device.open_path(path)
            else:
                self._device.open(self.vendor_id, self.product_id)
        except Exception as exc:  # noqa: BLE001
            self._device = None
            self.last_error = f"hid_open_failed:{exc}"
            return False
        self.last_error = ""
        return True

    def send(self, led_state: LedState) -> bool:
        if self._device is None:
            return False
        try:
            state_code = {
                "NO_LOCK": 0,
                "ACQUIRE": 1,
                "HANDOFF": 2,
                "LOCKED": 3,
                "HOLD": 4,
            }.get(led_state.state, 0)
            sector = int(led_state.sector if led_state.sector is not None else 0) & 0xFF
            bright = int(_clamp01(led_state.brightness) * 255.0) & 0xFF
            r, g, b = led_state.rgb
            # Placeholder packet format until UMA-8 LED report layout is finalized.
            packet = bytes(
                [
                    0x00,
                    state_code,
                    sector,
                    bright,
                    int(r) & 0xFF,
                    int(g) & 0xFF,
                    int(b) & 0xFF,
                    int(len(led_state.sectors)) & 0xFF,
                ]
            )
            self._device.write(packet)
        except Exception:  # noqa: BLE001
            return False
        return True

    def close(self) -> None:
        device = self._device
        self._device = None
        if device is None:
            return
        try:
            device.close()
        except Exception:  # noqa: BLE001
            return


def normalize_deg(angle_deg: float) -> float:
    return float(angle_deg) % 360.0


def bearing_to_sector(angle_deg: float, ring_size: int) -> int:
    ring = max(1, int(ring_size))
    step = 360.0 / float(ring)
    normalized = normalize_deg(angle_deg)
    return int(normalized // step) % ring


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _parse_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def _parse_rgb(cfg: Dict[str, Any], key: str, default: Tuple[int, int, int]) -> Tuple[int, int, int]:
    raw = cfg.get(key, list(default))
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return default
    vals: List[int] = []
    for item in raw:
        try:
            v = int(item)
        except Exception:  # noqa: BLE001
            v = 0
        vals.append(max(0, min(255, v)))
    return (vals[0], vals[1], vals[2])


def _extract_doa_peak_bearing(doa_msg: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(doa_msg, dict):
        return None
    peaks = doa_msg.get("peaks")
    if isinstance(peaks, list) and peaks:
        first = peaks[0]
        if isinstance(first, dict):
            angle = _parse_float(first.get("angle_deg"))
            if angle is not None:
                return normalize_deg(angle)
    heatmap = doa_msg.get("heatmap")
    bin_size = _parse_float(doa_msg.get("bin_size_deg"))
    if not isinstance(heatmap, list) or not heatmap or bin_size is None or bin_size <= 0:
        return None
    try:
        idx = max(range(len(heatmap)), key=lambda i: float(heatmap[i]))
    except Exception:  # noqa: BLE001
        return None
    return normalize_deg(float(idx) * float(bin_size))


def _smooth_angle(prev_deg: Optional[float], new_deg: float, alpha: float) -> float:
    alpha_clamped = _clamp01(alpha)
    if prev_deg is None:
        return normalize_deg(new_deg)
    prev_rad = math.radians(normalize_deg(prev_deg))
    new_rad = math.radians(normalize_deg(new_deg))
    delta = math.atan2(math.sin(new_rad - prev_rad), math.cos(new_rad - prev_rad))
    # alpha=1 follows new value strongly; alpha=0 keeps previous.
    smoothed = prev_rad + alpha_clamped * delta
    return normalize_deg(math.degrees(smoothed))


def _spread(center: int, ring_size: int, width: int) -> List[int]:
    ring = max(1, int(ring_size))
    w = max(0, int(width))
    out = {center % ring}
    for d in range(1, w + 1):
        out.add((center + d) % ring)
        out.add((center - d) % ring)
    return sorted(out)


def compute_led_state(
    cfg: Dict[str, Any],
    lock_msg: Dict[str, Any],
    smoothed_bearing_deg: Optional[float],
    search_phase: int,
    pulse_phase: float = 0.0,
) -> LedState:
    ring_size = max(1, int(cfg.get("ring_size", 12) or 12))
    state = str(lock_msg.get("state", "NO_LOCK") or "NO_LOCK").upper()

    base_offset = float(cfg.get("base_bearing_offset_deg", 0.0) or 0.0)
    source_bearing = _parse_float(lock_msg.get("target_bearing_deg"))

    mapped_bearing: Optional[float]
    if smoothed_bearing_deg is not None:
        mapped_bearing = normalize_deg(smoothed_bearing_deg + base_offset)
    elif source_bearing is not None:
        mapped_bearing = normalize_deg(source_bearing + base_offset)
    else:
        mapped_bearing = None

    sector = bearing_to_sector(mapped_bearing, ring_size) if mapped_bearing is not None else None

    brightness_min = _clamp01(float(cfg.get("brightness_min", 0.05) or 0.05))
    brightness_max = _clamp01(float(cfg.get("brightness_max", 0.85) or 0.85))
    if brightness_max < brightness_min:
        brightness_min, brightness_max = brightness_max, brightness_min

    idle_rgb = _parse_rgb(cfg, "idle_rgb", (10, 10, 24))
    lock_rgb = _parse_rgb(cfg, "lock_rgb", (0, 255, 128))
    search_rgb = _parse_rgb(cfg, "search_rgb", (0, 140, 255))

    if state in _LOCK_STATES and sector is not None:
        width = int(cfg.get("beam_width_leds", 1) or 1)
        sectors = _spread(sector, ring_size, width)
        rgb = lock_rgb
        brightness = brightness_max
    elif state in _ACQUIRE_STATES:
        center = sector if sector is not None else (search_phase % ring_size)
        width = int(cfg.get("search_width_leds", 1) or 1)
        sectors = _spread(center, ring_size, width)
        pulse = 0.5 * (1.0 + math.sin(2.0 * math.pi * float(pulse_phase)))
        rgb = search_rgb
        brightness = brightness_min + (brightness_max - brightness_min) * (0.35 + 0.65 * pulse)
    else:
        idle_on = bool(cfg.get("idle_on", True))
        if idle_on:
            # Keep a visible idle indicator even before first lock/target bearing.
            idle_sector = sector if sector is not None else (search_phase % ring_size)
            sectors = [idle_sector]
        else:
            sectors = []
        rgb = idle_rgb
        brightness = brightness_min if sectors else 0.0

    return LedState(
        enabled=True,
        backend=str(cfg.get("backend", "simulate") or "simulate"),
        preferred_backend=str(cfg.get("backend", "simulate") or "simulate"),
        state=state,
        source_bearing_deg=source_bearing,
        smoothed_bearing_deg=smoothed_bearing_deg,
        mapped_bearing_deg=mapped_bearing,
        sector=sector,
        sectors=sectors,
        brightness=float(brightness),
        rgb=rgb,
        ring_size=ring_size,
        base_bearing_offset_deg=base_offset,
        transport_error="",
        device_count=None,
    )


def build_transport(cfg: Dict[str, Any]) -> Uma8LedTransport:
    backend = str(cfg.get("backend", "simulate") or "simulate").strip().lower()
    if backend == "hid":
        return HidTransport(
            vendor_id=int(cfg.get("vendor_id", 0x2752) or 0x2752),
            product_id=int(cfg.get("product_id", 0x001C) or 0x001C),
        )
    if backend == "simulate":
        return SimulateTransport()
    return NoopTransport()


def _state_payload(led_state: LedState) -> Dict[str, Any]:
    return {
        "t_ns": now_ns(),
        "enabled": led_state.enabled,
        "backend": led_state.backend,
        "preferred_backend": led_state.preferred_backend,
        "state": led_state.state,
        "source_bearing_deg": led_state.source_bearing_deg,
        "smoothed_bearing_deg": led_state.smoothed_bearing_deg,
        "mapped_bearing_deg": led_state.mapped_bearing_deg,
        "sector": led_state.sector,
        "sectors": led_state.sectors,
        "brightness": led_state.brightness,
        "rgb": list(led_state.rgb),
        "ring_size": led_state.ring_size,
        "base_bearing_offset_deg": led_state.base_bearing_offset_deg,
        "transport_error": led_state.transport_error,
        "device_count": led_state.device_count,
    }


def _init_transport(cfg: Dict[str, Any], logger: Any, strict_transport: bool = False) -> Uma8LedTransport:
    preferred = str(cfg.get("backend", "simulate") or "simulate").strip().lower()
    fallback_enabled = bool(cfg.get("enabled_fallback", True))
    last_error = ""
    last_device_count: Optional[int] = None

    if strict_transport:
        candidates: List[str] = [preferred]
    else:
        candidates = [preferred]
        if fallback_enabled:
            if preferred == "hid":
                candidates.extend(["simulate", "none"])
            elif preferred == "simulate":
                candidates.append("none")

    seen = set()
    ordered_candidates: List[str] = []
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        ordered_candidates.append(cand)

    for backend in ordered_candidates:
        transport = build_transport({**cfg, "backend": backend})
        if transport.open():
            transport_error = ""
            if backend != preferred:
                transport_error = last_error
            setattr(transport, "_ff_transport_error", transport_error)
            setattr(transport, "_ff_device_count", last_device_count if last_device_count is not None else getattr(transport, "last_device_count", None))
            setattr(transport, "_ff_preferred_backend", preferred)
            logger.emit(
                "info",
                "uma8_leds",
                "transport_init_ok",
                {
                    "backend": transport.backend,
                    "vendor_id": int(cfg.get("vendor_id", 0x2752) or 0x2752),
                    "product_id": int(cfg.get("product_id", 0x001C) or 0x001C),
                },
            )
            if preferred == "hid" and backend == "simulate":
                logger.emit(
                    "warning",
                    "uma8_leds",
                    "transport_switched",
                    {
                        "from_backend": "hid",
                        "to_backend": "simulate",
                        "reason": "init_open_failed",
                        "error": transport_error,
                        "device_count": getattr(transport, "_ff_device_count", None),
                    },
                )
            return transport
        last_error = str(getattr(transport, "last_error", "") or "")
        last_device_count = getattr(transport, "last_device_count", None)
        logger.emit(
            "warning",
            "uma8_leds",
            "transport_unavailable",
            {
                "backend": backend,
                "reason": "open_failed",
                "error": getattr(transport, "last_error", ""),
                "device_count": getattr(transport, "last_device_count", None),
            },
        )
        if strict_transport and backend == preferred:
            logger.emit(
                "error",
                "uma8_leds",
                "transport_required_failed",
                {
                    "backend": backend,
                    "reason": "open_failed",
                    "error": str(getattr(transport, "last_error", "") or ""),
                    "device_count": getattr(transport, "last_device_count", None),
                },
            )
            raise RuntimeError(
                f"UMA8 strict transport failed to initialize backend={backend}: "
                f"{str(getattr(transport, 'last_error', '') or 'open_failed')}"
            )

    fallback = NoopTransport()
    fallback.open()
    setattr(fallback, "_ff_transport_error", last_error)
    setattr(fallback, "_ff_device_count", last_device_count)
    setattr(fallback, "_ff_preferred_backend", preferred)
    if strict_transport:
        raise RuntimeError(
            f"UMA8 strict transport failed; backend={preferred} unavailable: "
            f"{str(last_error or 'all_backends_failed')}"
        )
    logger.emit(
        "warning",
        "uma8_leds",
        "transport_unavailable",
        {
            "backend": preferred,
            "reason": "all_backends_failed",
        },
    )
    logger.emit("info", "uma8_leds", "transport_init_ok", {"backend": fallback.backend})
    return fallback


def start_uma8_led_service(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    cfg = config.get("uma8_leds", {})
    if not isinstance(cfg, dict):
        cfg = {}

    enabled = bool(cfg.get("enabled", False))
    update_hz = float(cfg.get("update_hz", 12.0) or 12.0)
    update_hz = max(1.0, update_hz)
    period = 1.0 / update_hz
    smoothing_alpha = _clamp01(float(cfg.get("smoothing_alpha", 0.35) or 0.35))
    strict_transport = bool(cfg.get("strict_transport", False))

    q_lock = bus.subscribe("fusion.target_lock")
    q_doa = bus.subscribe("audio.doa_heatmap")
    q_vad = bus.subscribe("audio.vad")
    doa_fallback_enabled = bool(cfg.get("doa_fallback_enabled", True))
    doa_min_confidence = float(cfg.get("doa_min_confidence", 0.05) or 0.05)
    doa_use_without_vad = bool(cfg.get("doa_use_without_vad", False))

    if not enabled:
        def _disabled() -> None:
            logger.emit("info", "uma8_leds", "disabled", {"reason": "config_disabled"})

        thread = threading.Thread(target=_disabled, name="uma8-leds", daemon=True)
        thread.start()
        return thread

    logger.emit(
        "info",
        "uma8_leds",
        "started",
        {
            "update_hz": update_hz,
            "backend": str(cfg.get("backend", "simulate") or "simulate"),
        },
    )

    transport = _init_transport(cfg, logger, strict_transport=strict_transport)
    fallback_enabled = bool(cfg.get("enabled_fallback", True))

    def _run() -> None:
        smoothed_bearing: Optional[float] = None
        latest_lock: Dict[str, Any] = {"state": "NO_LOCK", "target_bearing_deg": None}
        latest_doa: Optional[Dict[str, Any]] = None
        latest_vad: Optional[Dict[str, Any]] = None
        next_tick = time.monotonic()
        search_phase = 0
        pulse_phase = 0.0

        nonlocal transport
        preferred_backend = str(cfg.get("backend", "simulate") or "simulate").strip().lower()
        switched_from_hid_warned = False

        try:
            while not stop_event.is_set():
                # Drain lock queue so we always render from freshest lock state.
                try:
                    while True:
                        msg = q_lock.get_nowait()
                        if isinstance(msg, dict):
                            latest_lock = msg
                except queue.Empty:
                    pass
                try:
                    while True:
                        msg = q_doa.get_nowait()
                        if isinstance(msg, dict):
                            latest_doa = msg
                except queue.Empty:
                    pass
                try:
                    while True:
                        msg = q_vad.get_nowait()
                        if isinstance(msg, dict):
                            latest_vad = msg
                except queue.Empty:
                    pass

                now = time.monotonic()
                if now < next_tick:
                    time.sleep(min(0.02, next_tick - now))
                    continue

                next_tick = now + period
                search_phase = (search_phase + 1) % 1024
                pulse_phase = (pulse_phase + period * 1.25) % 1.0

                lock_for_led = latest_lock
                lock_state = str(lock_for_led.get("state", "NO_LOCK") or "NO_LOCK").upper()
                source_bearing = _parse_float(lock_for_led.get("target_bearing_deg"))
                if doa_fallback_enabled and source_bearing is None:
                    doa_conf = _parse_float((latest_doa or {}).get("confidence"))
                    doa_bearing = _extract_doa_peak_bearing(latest_doa)
                    vad_speech = bool((latest_vad or {}).get("speech"))
                    if (
                        doa_bearing is not None
                        and doa_conf is not None
                        and doa_conf >= doa_min_confidence
                        and (vad_speech or doa_use_without_vad)
                    ):
                        lock_for_led = dict(latest_lock)
                        lock_for_led["target_bearing_deg"] = doa_bearing
                        if lock_state in _NO_LOCK_STATES:
                            lock_for_led["state"] = "ACQUIRE"
                            lock_state = "ACQUIRE"
                        source_bearing = doa_bearing
                if lock_state in _NO_LOCK_STATES or source_bearing is None:
                    smoothed_bearing = None
                else:
                    smoothed_bearing = _smooth_angle(smoothed_bearing, source_bearing, smoothing_alpha)

                led_state = compute_led_state(
                    cfg,
                    lock_for_led,
                    smoothed_bearing_deg=smoothed_bearing,
                    search_phase=search_phase,
                    pulse_phase=pulse_phase,
                )
                # Report the actual active transport backend (hid/simulate/none) in telemetry.
                led_state.backend = transport.backend
                led_state.preferred_backend = str(getattr(transport, "_ff_preferred_backend", preferred_backend))
                led_state.transport_error = str(getattr(transport, "_ff_transport_error", "") or "")
                led_state.device_count = getattr(transport, "_ff_device_count", None)

                sent = transport.send(led_state)
                if not sent:
                    send_error = str(getattr(transport, "last_error", "") or "send_failed")
                    send_device_count = getattr(transport, "last_device_count", getattr(transport, "_ff_device_count", None))
                    logger.emit(
                        "warning",
                        "uma8_leds",
                        "transport_unavailable",
                        {
                            "backend": transport.backend,
                            "reason": "send_failed",
                            "error": send_error,
                            "device_count": send_device_count,
                        },
                    )
                    if strict_transport and transport.backend == preferred_backend:
                        logger.emit(
                            "error",
                            "uma8_leds",
                            "transport_required_failed",
                            {
                                "backend": transport.backend,
                                "reason": "send_failed",
                                "error": send_error,
                                "device_count": send_device_count,
                            },
                        )
                        raise RuntimeError(
                            f"UMA8 strict transport send failed backend={transport.backend}: {send_error}"
                        )
                    if fallback_enabled and transport.backend == "hid":
                        try:
                            transport.close()
                        except Exception:  # noqa: BLE001
                            pass
                        transport = build_transport({**cfg, "backend": "simulate"})
                        if not transport.open():
                            transport = NoopTransport()
                            transport.open()
                        setattr(transport, "_ff_transport_error", f"hid_send_failed:{send_error}")
                        setattr(transport, "_ff_device_count", send_device_count)
                        setattr(transport, "_ff_preferred_backend", preferred_backend)
                        if not switched_from_hid_warned:
                            logger.emit(
                                "warning",
                                "uma8_leds",
                                "transport_switched",
                                {
                                    "from_backend": "hid",
                                    "to_backend": transport.backend,
                                    "reason": "send_failed",
                                    "error": send_error,
                                    "device_count": send_device_count,
                                },
                            )
                            switched_from_hid_warned = True
                        logger.emit("info", "uma8_leds", "transport_init_ok", {"backend": transport.backend})

                payload = _state_payload(led_state)
                bus.publish("uma8_leds.state", payload)
                logger.emit("debug", "uma8_leds", "frame_sent", payload)
        except Exception as exc:  # noqa: BLE001
            logger.emit("warning", "uma8_leds", "disabled", {"reason": "thread_failure", "error": str(exc)})
            if strict_transport:
                raise
        finally:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=_run, name="uma8-leds", daemon=True)
    thread.start()
    return thread


__all__ = [
    "LedState",
    "Uma8LedTransport",
    "NoopTransport",
    "SimulateTransport",
    "HidTransport",
    "build_transport",
    "normalize_deg",
    "bearing_to_sector",
    "compute_led_state",
    "start_uma8_led_service",
]

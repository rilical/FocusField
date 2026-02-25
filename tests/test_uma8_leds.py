import queue
import threading
import time
import unittest
from unittest.mock import patch

from focusfield.uma8.led_control import (
    HidTransport,
    SimulateTransport,
    bearing_to_sector,
    compute_led_state,
    start_uma8_led_service,
)


class _FakeBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[queue.Queue]] = {}
        self.published: list[tuple[str, dict]] = []

    def subscribe(self, topic: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        self._subs.setdefault(topic, []).append(q)
        return q

    def publish(self, topic: str, msg: dict) -> None:
        self.published.append((topic, msg))
        for q in self._subs.get(topic, []):
            q.put(msg)


class _FakeLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict]] = []

    def emit(self, level, module, event, payload):  # noqa: ANN001
        self.events.append((level, module, event, payload))


class Uma8LedTests(unittest.TestCase):
    def _cfg(self) -> dict:
        return {
            "uma8_leds": {
                "enabled": True,
                "enabled_fallback": True,
                "backend": "simulate",
                "ring_size": 12,
                "update_hz": 25,
                "base_bearing_offset_deg": 0,
                "idle_rgb": [10, 10, 24],
                "lock_rgb": [0, 255, 128],
                "search_rgb": [0, 140, 255],
                "smoothing_alpha": 0.35,
                "brightness_min": 0.05,
                "brightness_max": 0.85,
                "vendor_id": 0x2752,
                "product_id": 0x001C,
            }
        }

    def test_bearing_to_sector_edges(self) -> None:
        self.assertEqual(bearing_to_sector(0.0, 12), 0)
        self.assertEqual(bearing_to_sector(29.9, 12), 0)
        self.assertEqual(bearing_to_sector(30.0, 12), 1)
        self.assertEqual(bearing_to_sector(359.0, 12), 11)

    def test_state_behavior_matrix(self) -> None:
        cfg = self._cfg()["uma8_leds"]

        no_lock = compute_led_state(cfg, {"state": "NO_LOCK"}, None, search_phase=1, pulse_phase=0.1)
        self.assertEqual(no_lock.state, "NO_LOCK")
        self.assertEqual(no_lock.sectors, [])

        acquire = compute_led_state(
            cfg,
            {"state": "ACQUIRE", "target_bearing_deg": 65.0},
            65.0,
            search_phase=2,
            pulse_phase=0.3,
        )
        self.assertEqual(acquire.state, "ACQUIRE")
        self.assertTrue(len(acquire.sectors) >= 1)

        locked = compute_led_state(
            cfg,
            {"state": "LOCKED", "target_bearing_deg": 125.0},
            125.0,
            search_phase=0,
            pulse_phase=0.0,
        )
        self.assertEqual(locked.state, "LOCKED")
        self.assertTrue(len(locked.sectors) >= 1)
        self.assertGreaterEqual(locked.brightness, cfg["brightness_min"])

        hold = compute_led_state(
            cfg,
            {"state": "HOLD", "target_bearing_deg": 125.0},
            125.0,
            search_phase=0,
            pulse_phase=0.0,
        )
        self.assertEqual(hold.state, "HOLD")
        self.assertTrue(len(hold.sectors) >= 1)

    def test_disabled_service_is_non_fatal(self) -> None:
        bus = _FakeBus()
        logger = _FakeLogger()
        stop_event = threading.Event()

        thread = start_uma8_led_service(bus, {"uma8_leds": {"enabled": False}}, logger, stop_event)
        thread.join(timeout=1.0)

        self.assertTrue(any(event == "disabled" for _, _, event, _ in logger.events))

    def test_hid_unavailable_falls_back(self) -> None:
        bus = _FakeBus()
        logger = _FakeLogger()
        stop_event = threading.Event()
        cfg = self._cfg()
        cfg["uma8_leds"]["backend"] = "hid"
        cfg["uma8_leds"]["enabled_fallback"] = True

        with patch.object(HidTransport, "open", return_value=False):
            thread = start_uma8_led_service(bus, cfg, logger, stop_event)
            bus.publish("fusion.target_lock", {"state": "LOCKED", "target_bearing_deg": 30.0})
            time.sleep(0.15)
            stop_event.set()
            thread.join(timeout=1.0)

        self.assertTrue(any(event == "transport_unavailable" for _, _, event, _ in logger.events))
        self.assertTrue(any(event == "transport_init_ok" and payload.get("backend") == "simulate" for _, _, event, payload in logger.events))

    def test_service_publishes_led_state_from_lock_stream(self) -> None:
        bus = _FakeBus()
        logger = _FakeLogger()
        stop_event = threading.Event()
        cfg = self._cfg()

        transport = SimulateTransport()
        with patch("focusfield.uma8.led_control.build_transport", return_value=transport):
            thread = start_uma8_led_service(bus, cfg, logger, stop_event)
            bus.publish("fusion.target_lock", {"state": "LOCKED", "target_bearing_deg": 35.0})
            time.sleep(0.20)
            stop_event.set()
            thread.join(timeout=1.0)

        self.assertGreater(len(transport.history), 0)
        latest = transport.history[-1]
        self.assertEqual(latest.sector, 1)

        led_msgs = [msg for topic, msg in bus.published if topic == "uma8_leds.state"]
        self.assertGreater(len(led_msgs), 0)
        self.assertEqual(led_msgs[-1].get("state"), "LOCKED")


if __name__ == "__main__":
    unittest.main()

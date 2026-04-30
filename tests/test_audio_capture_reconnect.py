import unittest

from focusfield.audio.capture import _stream_stalled  # noqa: PLC2701


class AudioCaptureReconnectTests(unittest.TestCase):
    def test_stream_stalls_when_callbacks_stop_past_timeout(self) -> None:
        self.assertTrue(_stream_stalled(last_callback_s=10.0, now_s=13.1, timeout_s=3.0))

    def test_stream_does_not_stall_while_callbacks_are_recent(self) -> None:
        self.assertFalse(_stream_stalled(last_callback_s=10.0, now_s=12.9, timeout_s=3.0))

    def test_stream_stall_watchdog_can_be_disabled(self) -> None:
        self.assertFalse(_stream_stalled(last_callback_s=10.0, now_s=100.0, timeout_s=0.0))


if __name__ == "__main__":
    unittest.main()

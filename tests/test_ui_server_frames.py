import unittest

import numpy as np

from focusfield.ui.server import UIState


class UIStateFrameTests(unittest.TestCase):
    def test_stale_frame_jpeg_expires(self) -> None:
        state = UIState(frame_stale_after_s=1.0)
        frame = np.zeros((8, 8, 3), dtype=np.uint8)

        state.update_frame("cam1", frame, jpeg_quality=65)
        self.assertIsNotNone(state.get_frame_jpeg("cam1"))

        with state._lock:  # noqa: SLF001 - focused clock-age test for frame expiry.
            state._frame_encode_ns["cam1"] -= 2_000_000_000  # noqa: SLF001
        self.assertIsNone(state.get_frame_jpeg("cam1"))


if __name__ == "__main__":
    unittest.main()

import unittest

import numpy as np

from focusfield.audio.vad import _to_mono  # noqa: PLC2701


class AudioVadTests(unittest.TestCase):
    def test_to_mono_uses_strongest_channel_for_multichannel_arrays(self) -> None:
        quiet = np.full(8, 0.01, dtype=np.float32)
        inverted = -quiet
        speech = np.linspace(-0.4, 0.4, 8, dtype=np.float32)
        frame = np.stack([quiet, speech, inverted], axis=1)

        mono = _to_mono(frame)

        self.assertTrue(np.allclose(mono, speech))

    def test_to_mono_falls_back_to_mean_for_silent_multichannel_arrays(self) -> None:
        frame = np.zeros((8, 3), dtype=np.float32)

        mono = _to_mono(frame)

        self.assertTrue(np.allclose(mono, np.zeros(8, dtype=np.float32)))


if __name__ == "__main__":
    unittest.main()

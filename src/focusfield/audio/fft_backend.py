from __future__ import annotations

import os
from typing import Any, Optional

import numpy as np

try:
    import pyfftw
    import pyfftw.interfaces.numpy_fft as fft_impl

    pyfftw.interfaces.cache.enable()
    pyfftw.interfaces.cache.set_keepalive_time(30.0)
    _HAS_PYFFTW = True
except ImportError:  # pragma: no cover
    pyfftw = None
    fft_impl = None
    _HAS_PYFFTW = False


_FFTW_THREADS = max(1, int(os.environ.get("FOCUSFIELD_FFTW_THREADS", "1") or 1))


def backend_name() -> str:
    return "pyfftw" if _HAS_PYFFTW else "numpy"


def rfft(a: Any, n: Optional[int] = None, axis: int = -1) -> np.ndarray:
    if _HAS_PYFFTW and fft_impl is not None:
        return fft_impl.rfft(a, n=n, axis=axis, threads=_FFTW_THREADS)
    return np.fft.rfft(a, n=n, axis=axis)


def irfft(a: Any, n: Optional[int] = None, axis: int = -1) -> np.ndarray:
    if _HAS_PYFFTW and fft_impl is not None:
        return fft_impl.irfft(a, n=n, axis=axis, threads=_FFTW_THREADS)
    return np.fft.irfft(a, n=n, axis=axis)


def rfftfreq(n: int, d: float = 1.0) -> np.ndarray:
    return np.fft.rfftfreq(n, d=d)

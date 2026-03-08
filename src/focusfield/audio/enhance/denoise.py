"""focusfield.audio.enhance.denoise

CONTRACT: inline (source: src/focusfield/audio/enhance/denoise.md)
ROLE: Optional post-beam denoise stage.

INPUTS:
  - Topic: audio.enhanced.beamformed  Type: EnhancedAudio
OUTPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio

CONFIG KEYS:
  - audio.denoise.enabled: enable denoise
  - audio.denoise.backend: wiener|rnnoise|rnnoise_native|rnnoise_onnx|hybrid
  - audio.denoise.wiener.g_min: minimum gain
  - audio.denoise.wiener.noise_ema_alpha: noise PSD smoothing

PERF / TIMING:
  - per-frame rFFT/irFFT for wiener
  - per-frame ONNX inference when rnnoise_onnx is enabled

FAILURE MODES:
  - backend error -> fallback or bypass -> log denoise_failed

LOG EVENTS:
  - module=audio.enhance.denoise, event=denoise_failed, payload keys=backend, error
"""

from __future__ import annotations

import queue
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from focusfield.audio.fft_backend import irfft, rfft
try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover
    ort = None

try:
    from pyrnnoise import RNNoise as NativeRNNoise
except ImportError:  # pragma: no cover
    NativeRNNoise = None

from focusfield.core.clock import now_ns


@dataclass
class _WienerState:
    nfft: int
    noise_psd: Optional[np.ndarray] = None


@dataclass
class _RnnoiseState:
    nfft: int
    noise_psd: Optional[np.ndarray] = None
    gain_ema: Optional[np.ndarray] = None


@dataclass
class _RnnoiseOnnxState:
    frame_size: int
    sample_rate_hz: int
    model_path: str = ""
    model_url: str = ""
    allow_fallback: bool = True
    session: Any = None
    audio_input_name: str = ""
    audio_input_rank: int = 2
    audio_output_name: str = ""
    state_inputs: List[str] = field(default_factory=list)
    state_outputs_for_inputs: Dict[str, str] = field(default_factory=dict)
    state_tensors: Dict[str, np.ndarray] = field(default_factory=dict)
    warned_missing_model: bool = False
    warned_init_failed: bool = False


@dataclass
class _RnnoiseNativeState:
    sample_rate_hz: int
    engine: Any = None
    warned_missing: bool = False
    warned_failed: bool = False


def start_denoise(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    denoise_cfg = config.get("audio", {}).get("denoise", {})
    if not isinstance(denoise_cfg, dict):
        denoise_cfg = {}
    if not bool(denoise_cfg.get("enabled", False)):
        return None
    backend = str(denoise_cfg.get("backend", "wiener")).lower()

    wiener_cfg = denoise_cfg.get("wiener", {})
    if not isinstance(wiener_cfg, dict):
        wiener_cfg = {}
    g_min = float(wiener_cfg.get("g_min", 0.05))
    noise_alpha = float(wiener_cfg.get("noise_ema_alpha", 0.98))
    nfft = int(wiener_cfg.get("nfft", config.get("audio", {}).get("block_size", 1024)))
    nfft = max(256, int(2 ** round(np.log2(max(256, nfft)))))
    state = _WienerState(nfft=nfft)

    rnnoise_cfg = denoise_cfg.get("rnnoise", {})
    if not isinstance(rnnoise_cfg, dict):
        rnnoise_cfg = {}
    rnnoise_nfft = int(rnnoise_cfg.get("nfft", nfft))
    rnnoise_nfft = max(256, int(2 ** round(np.log2(max(256, rnnoise_nfft)))))
    rnnoise_strength = float(rnnoise_cfg.get("strength", 0.65))
    rnnoise_min_gain = float(rnnoise_cfg.get("min_gain", 0.08))
    rnnoise_noise_alpha = float(rnnoise_cfg.get("noise_ema_alpha", 0.98))
    rnnoise_gain_alpha = float(rnnoise_cfg.get("gain_ema_alpha", 0.85))
    rnnoise_state = _RnnoiseState(nfft=rnnoise_nfft)
    rnnoise_native_state = _RnnoiseNativeState(sample_rate_hz=int(config.get("audio", {}).get("sample_rate_hz", 48000) or 48000))
    rnnoise_onnx_state = _RnnoiseOnnxState(
        frame_size=max(120, int(rnnoise_cfg.get("frame_size", 480) or 480)),
        sample_rate_hz=int(config.get("audio", {}).get("sample_rate_hz", 48000) or 48000),
        model_path=str(rnnoise_cfg.get("model_path", "") or ""),
        model_url=str(rnnoise_cfg.get("model_url", "") or ""),
        allow_fallback=bool(rnnoise_cfg.get("allow_fallback", True)),
    )

    hybrid_cfg = denoise_cfg.get("hybrid", {})
    if not isinstance(hybrid_cfg, dict):
        hybrid_cfg = {}
    hybrid_strength = float(hybrid_cfg.get("postfilter_strength", 0.5))
    hybrid_strength = float(min(1.0, max(0.0, hybrid_strength)))

    if backend not in {"wiener", "rnnoise", "rnnoise_native", "rnnoise_onnx", "hybrid"}:
        logger.emit("warning", "audio.enhance.denoise", "denoise_failed", {"backend": backend, "error": "unsupported_backend"})
        return None

    q_in = bus.subscribe("audio.enhanced.beamformed")
    q_vad = bus.subscribe("audio.vad")
    last_vad: Optional[Dict[str, Any]] = None

    def _drain_latest(q: queue.Queue) -> Optional[Dict[str, Any]]:
        item = None
        try:
            while True:
                item = q.get_nowait()
        except queue.Empty:
            pass
        return item

    def _apply_rnnoise_onnx(y_in: np.ndarray, speech: bool, sample_rate_hz: int) -> np.ndarray:
        return _rnnoise_onnx_denoise(
            y_in,
            rnnoise_onnx_state,
            logger=logger,
            backend_name=backend,
            speech=speech,
            sample_rate_hz=sample_rate_hz,
            fallback=lambda x: _apply_rnnoise_native_or_spectral(x, sample_rate_hz, speech),
        )

    def _apply_rnnoise_native_or_spectral(y_in: np.ndarray, sample_rate_hz: int, speech: bool) -> np.ndarray:
        native = _rnnoise_native_denoise(y_in, rnnoise_native_state, logger, backend, sample_rate_hz)
        if native is not None:
            return native
        return _rnnoise_like_denoise(
            y_in,
            rnnoise_state,
            speech=speech,
            noise_ema_alpha=rnnoise_noise_alpha,
            gain_ema_alpha=rnnoise_gain_alpha,
            strength=rnnoise_strength,
            min_gain=rnnoise_min_gain,
        )

    def _run() -> None:
        nonlocal last_vad
        seq_out = 0
        while not stop_event.is_set():
            last_vad = _drain_latest(q_vad) or last_vad
            try:
                msg_in = q_in.get(timeout=0.1)
            except queue.Empty:
                continue
            data = msg_in.get("data")
            if data is None:
                continue
            x = np.asarray(data).astype(np.float32)
            if x.ndim != 1:
                x = x.reshape(-1)

            speech_prob = 0.0
            if last_vad:
                speech_prob = float(last_vad.get("speech_probability", 1.0 if last_vad.get("speech") else 0.0) or 0.0)
            speech = speech_prob >= 0.5
            sample_rate_hz = int(msg_in.get("sample_rate_hz", rnnoise_onnx_state.sample_rate_hz))

            y = x
            if backend == "wiener":
                y = _wiener_denoise(y, state, speech, noise_alpha, g_min)
            elif backend == "rnnoise":
                y = _rnnoise_like_denoise(
                    y,
                    rnnoise_state,
                    speech=speech,
                    noise_ema_alpha=rnnoise_noise_alpha,
                    gain_ema_alpha=rnnoise_gain_alpha,
                    strength=rnnoise_strength,
                    min_gain=rnnoise_min_gain,
                )
            elif backend == "rnnoise_native":
                y = _apply_rnnoise_native_or_spectral(y, sample_rate_hz, speech)
            elif backend == "rnnoise_onnx":
                y = _apply_rnnoise_onnx(y, speech, sample_rate_hz)
            else:  # hybrid
                y = _wiener_denoise(y, state, speech, noise_alpha, g_min)
                y_hybrid = _apply_rnnoise_onnx(y, speech, sample_rate_hz)
                y = (1.0 - hybrid_strength) * y + hybrid_strength * y_hybrid

            seq_out += 1
            bus.publish(
                "audio.enhanced.final",
                {
                    "t_ns": int(msg_in.get("t_ns", now_ns())),
                    "seq": seq_out,
                    "sample_rate_hz": sample_rate_hz,
                    "frame_samples": int(y.shape[0]),
                    "channels": 1,
                    "data": y.astype(np.float32),
                    "stats": {
                        "rms": float(np.sqrt(np.mean(y**2))) if y.size else 0.0,
                        "backend": backend,
                    },
                },
            )

    thread = threading.Thread(target=_run, name="audio-denoise", daemon=True)
    thread.start()
    return thread


def _wiener_denoise(
    x: np.ndarray,
    state: _WienerState,
    speech: bool,
    noise_alpha: float,
    g_min: float,
) -> np.ndarray:
    if x.size == 0:
        return x
    nfft = state.nfft
    x_fft = rfft(x, n=nfft)
    psd = (np.abs(x_fft) ** 2).astype(np.float32)
    if (not speech) or state.noise_psd is None:
        if state.noise_psd is None:
            state.noise_psd = psd
        else:
            state.noise_psd = noise_alpha * state.noise_psd + (1.0 - noise_alpha) * psd
    noise = state.noise_psd if state.noise_psd is not None else psd
    gain = 1.0 - (noise / (psd + 1e-12))
    gain = np.clip(gain, float(g_min), 1.0).astype(np.float32)
    y_fft = x_fft * gain
    y = irfft(y_fft, n=nfft).astype(np.float32)
    return y[: x.shape[0]]


def _rnnoise_like_denoise(
    x: np.ndarray,
    state: _RnnoiseState,
    speech: bool,
    noise_ema_alpha: float,
    gain_ema_alpha: float,
    strength: float,
    min_gain: float,
) -> np.ndarray:
    """Dependency-light spectral suppression fallback."""
    if x.size == 0:
        return x

    nfft = state.nfft
    x_fft = rfft(x, n=nfft)
    mag = np.abs(x_fft).astype(np.float32)
    psd = (mag**2).astype(np.float32)

    if state.noise_psd is None:
        state.noise_psd = psd.copy()
    if not speech:
        state.noise_psd = noise_ema_alpha * state.noise_psd + (1.0 - noise_ema_alpha) * psd
    else:
        state.noise_psd = np.minimum(state.noise_psd, psd * 1.25)

    noise = np.maximum(state.noise_psd, 1e-12)
    snr = np.maximum(psd - noise, 0.0) / noise
    logistic = 1.0 / (1.0 + np.exp(-(snr - 1.0)))
    target_gain = min_gain + (1.0 - min_gain) * np.power(logistic, max(0.1, 1.0 + 2.0 * strength))
    target_gain = np.clip(target_gain, min_gain, 1.0).astype(np.float32)

    if state.gain_ema is None:
        state.gain_ema = target_gain
    state.gain_ema = gain_ema_alpha * state.gain_ema + (1.0 - gain_ema_alpha) * target_gain

    y_fft = x_fft * state.gain_ema.astype(np.complex64)
    y = irfft(y_fft, n=nfft).astype(np.float32)
    return y[: x.shape[0]]


def _rnnoise_onnx_denoise(
    x: np.ndarray,
    state: _RnnoiseOnnxState,
    logger: Any,
    backend_name: str,
    speech: bool,
    sample_rate_hz: int,
    fallback,
) -> np.ndarray:
    if x.size == 0:
        return x
    if sample_rate_hz != state.sample_rate_hz:
        return fallback(x) if state.allow_fallback else x
    if not _ensure_rnnoise_onnx_session(state, logger, backend_name):
        return fallback(x) if state.allow_fallback else x

    frame_size = int(state.frame_size)
    outputs: List[np.ndarray] = []
    for start in range(0, x.shape[0], frame_size):
        chunk = np.asarray(x[start : start + frame_size], dtype=np.float32)
        if chunk.shape[0] < frame_size:
            padded = np.zeros(frame_size, dtype=np.float32)
            padded[: chunk.shape[0]] = chunk
            chunk = padded
        denoised = _run_rnnoise_onnx_frame(chunk, state, logger, backend_name)
        if denoised is None:
            return fallback(x) if state.allow_fallback else x
        outputs.append(denoised[: min(frame_size, x.shape[0] - start)])
        if not speech and state.state_tensors:
            for name, tensor in list(state.state_tensors.items()):
                state.state_tensors[name] = 0.92 * tensor
    if not outputs:
        return x
    return np.concatenate(outputs, axis=0).astype(np.float32, copy=False)


def _rnnoise_native_denoise(
    x: np.ndarray,
    state: _RnnoiseNativeState,
    logger: Any,
    backend_name: str,
    sample_rate_hz: int,
) -> Optional[np.ndarray]:
    if NativeRNNoise is None:
        if not state.warned_missing:
            logger.emit(
                "warning",
                "audio.enhance.denoise",
                "denoise_failed",
                {"backend": backend_name, "error": "pyrnnoise_missing"},
            )
            state.warned_missing = True
        return None
    try:
        if state.engine is None or state.sample_rate_hz != sample_rate_hz:
            state.engine = NativeRNNoise(sample_rate=sample_rate_hz)
            state.sample_rate_hz = sample_rate_hz
        chunk = np.asarray(x, dtype=np.float32).reshape(1, -1)
        frames: List[np.ndarray] = []
        for _speech_prob, frame in state.engine.denoise_chunk(chunk, partial=True):
            frames.append(np.asarray(frame).reshape(-1))
        if not frames:
            return x
        return np.concatenate(frames, axis=0).astype(np.float32, copy=False)
    except Exception as exc:  # noqa: BLE001
        if not state.warned_failed:
            logger.emit(
                "warning",
                "audio.enhance.denoise",
                "denoise_failed",
                {"backend": backend_name, "error": f"pyrnnoise_failed:{exc}"},
            )
            state.warned_failed = True
        state.engine = None
        return None


def _ensure_rnnoise_onnx_session(state: _RnnoiseOnnxState, logger: Any, backend_name: str) -> bool:
    if state.session is not None:
        return True
    if ort is None:
        if not state.warned_init_failed:
            logger.emit(
                "warning",
                "audio.enhance.denoise",
                "denoise_failed",
                {"backend": backend_name, "error": "onnxruntime_missing"},
            )
            state.warned_init_failed = True
        return False

    try:
        model_path = _ensure_optional_model_path(state.model_path, state.model_url, "rnnoise.onnx")
    except Exception as exc:  # noqa: BLE001
        if not state.warned_missing_model:
            logger.emit(
                "warning",
                "audio.enhance.denoise",
                "denoise_failed",
                {"backend": backend_name, "error": str(exc)},
            )
            state.warned_missing_model = True
        return False

    if not model_path or not Path(model_path).exists():
        if not state.warned_missing_model:
            logger.emit(
                "warning",
                "audio.enhance.denoise",
                "denoise_failed",
                {"backend": backend_name, "error": "rnnoise_onnx_model_missing"},
            )
            state.warned_missing_model = True
        return False

    try:
        session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        state.session = session
        state.model_path = model_path
        inputs = list(session.get_inputs())
        outputs = list(session.get_outputs())
        audio_input = _select_audio_tensor(inputs, preferred_size=state.frame_size)
        audio_output = _select_audio_tensor(outputs, preferred_size=state.frame_size)
        if audio_input is None or audio_output is None:
            raise RuntimeError("unable to identify audio input/output")
        state.audio_input_name = str(audio_input.name)
        state.audio_input_rank = len(getattr(audio_input, "shape", []) or [])
        state.audio_output_name = str(audio_output.name)
        input_shape = getattr(audio_input, "shape", []) or []
        if input_shape:
            last_dim = input_shape[-1]
            if isinstance(last_dim, int) and last_dim > 0:
                state.frame_size = int(last_dim)

        state_inputs = [str(inp.name) for inp in inputs if str(inp.name) != state.audio_input_name]
        state_outputs = [str(out.name) for out in outputs if str(out.name) != state.audio_output_name]
        state.state_inputs = state_inputs
        state.state_outputs_for_inputs = _match_state_outputs(state_inputs, state_outputs)
        for inp in inputs:
            if str(inp.name) == state.audio_input_name:
                continue
            shape = _concrete_shape(getattr(inp, "shape", []) or [1])
            state.state_tensors[str(inp.name)] = np.zeros(shape, dtype=np.float32)
    except Exception as exc:  # noqa: BLE001
        state.session = None
        if not state.warned_init_failed:
            logger.emit(
                "warning",
                "audio.enhance.denoise",
                "denoise_failed",
                {"backend": backend_name, "error": f"rnnoise_onnx_init_failed:{exc}"},
            )
            state.warned_init_failed = True
        return False
    return True


def _run_rnnoise_onnx_frame(
    frame: np.ndarray,
    state: _RnnoiseOnnxState,
    logger: Any,
    backend_name: str,
) -> Optional[np.ndarray]:
    if state.session is None:
        return None
    feed: Dict[str, Any] = {state.audio_input_name: _reshape_audio_input(frame, state.audio_input_rank)}
    for name, tensor in state.state_tensors.items():
        feed[name] = tensor
    try:
        output_names = [out.name for out in state.session.get_outputs()]
        results = state.session.run(output_names, feed)
    except Exception as exc:  # noqa: BLE001
        logger.emit(
            "warning",
            "audio.enhance.denoise",
            "denoise_failed",
            {"backend": backend_name, "error": f"rnnoise_onnx_infer_failed:{exc}"},
        )
        state.session = None
        return None

    outputs = {name: value for name, value in zip(output_names, results)}
    audio = outputs.get(state.audio_output_name)
    if audio is None:
        return None
    for input_name, output_name in state.state_outputs_for_inputs.items():
        if output_name in outputs:
            state.state_tensors[input_name] = np.asarray(outputs[output_name], dtype=np.float32)
    return np.asarray(audio, dtype=np.float32).reshape(-1)


def _ensure_optional_model_path(model_path: str, model_url: str, default_filename: str) -> str:
    if model_path:
        path = Path(model_path).expanduser()
        if path.exists():
            return str(path)
        raise RuntimeError(f"model_path_missing:{path}")

    cache_dir = Path.home() / ".cache" / "focusfield"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / default_filename
    if path.exists():
        return str(path)
    if not model_url:
        return ""
    urllib.request.urlretrieve(model_url, path)
    return str(path)


def _select_audio_tensor(tensors: List[Any], preferred_size: int) -> Optional[Any]:
    best = None
    best_score = -1
    for tensor in tensors:
        shape = getattr(tensor, "shape", []) or []
        dims = [dim for dim in shape if isinstance(dim, int) and dim > 0]
        if not dims:
            score = 0
        else:
            last = dims[-1]
            score = 5 if last == preferred_size else 2 if last > 64 else 1
        if score > best_score:
            best = tensor
            best_score = score
    return best


def _match_state_outputs(inputs: List[str], outputs: List[str]) -> Dict[str, str]:
    remaining = list(outputs)
    matched: Dict[str, str] = {}
    for input_name in inputs:
        chosen = None
        normalized = input_name.replace("_in", "").replace("in_", "").replace("input", "")
        for output_name in remaining:
            if output_name == input_name:
                chosen = output_name
                break
            output_norm = output_name.replace("_out", "").replace("out_", "").replace("output", "")
            if output_norm == normalized or normalized in output_norm or output_norm in normalized:
                chosen = output_name
                break
        if chosen is None and remaining:
            chosen = remaining[0]
        if chosen is not None:
            matched[input_name] = chosen
            if chosen in remaining:
                remaining.remove(chosen)
    return matched


def _concrete_shape(shape: List[Any]) -> List[int]:
    dims: List[int] = []
    for dim in shape:
        if isinstance(dim, int) and dim > 0:
            dims.append(int(dim))
        else:
            dims.append(1)
    return dims or [1]


def _reshape_audio_input(frame: np.ndarray, rank: int) -> np.ndarray:
    if rank <= 1:
        return frame.astype(np.float32, copy=False)
    if rank == 2:
        return frame.reshape(1, -1).astype(np.float32, copy=False)
    if rank == 3:
        return frame.reshape(1, 1, -1).astype(np.float32, copy=False)
    shape = [1] * max(0, rank - 1) + [frame.shape[0]]
    return frame.reshape(shape).astype(np.float32, copy=False)

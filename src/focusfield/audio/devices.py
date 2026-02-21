"""focusfield.audio.devices

CONTRACT: inline (source: src/focusfield/audio/devices.md)
ROLE: Enumerate audio devices and resolve selected device.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - audio.device_index: explicit device index override (optional)
  - audio.device_id: preferred device id/name (optional)
  - audio.channels: required input channels
  - audio.device_selector.match_substring: substring to match in device name (optional)
  - audio.device_selector.require_input_channels: required input channels for selector (optional)

PERF / TIMING:
  - enumerate once at startup

FAILURE MODES:
  - no matching device -> raise -> log device_not_found

LOG EVENTS:
  - module=audio.devices, event=device_not_found, payload keys=criteria
  - module=audio.devices, event=device_selected, payload keys=device

TESTS:
  - n/a
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None


@dataclass(frozen=True)
class AudioDeviceInfo:
    index: int
    name: str
    hostapi: Optional[str]
    max_input_channels: int
    default_samplerate_hz: Optional[float]


def list_input_devices() -> List[AudioDeviceInfo]:
    """Return a normalized list of input-capable audio devices."""
    if sd is None:
        return []
    hostapis = []
    try:
        hostapis = sd.query_hostapis()
    except Exception:  # noqa: BLE001
        hostapis = []
    hostapi_names = {idx: api.get("name") for idx, api in enumerate(hostapis) if isinstance(api, dict)}

    devices: List[AudioDeviceInfo] = []
    for idx, raw in enumerate(sd.query_devices()):
        if not isinstance(raw, dict):
            continue
        max_in = int(raw.get("max_input_channels") or 0)
        if max_in <= 0:
            continue
        hostapi_idx = raw.get("hostapi")
        hostapi_name = hostapi_names.get(hostapi_idx) if isinstance(hostapi_idx, int) else None
        devices.append(
            AudioDeviceInfo(
                index=idx,
                name=str(raw.get("name") or ""),
                hostapi=hostapi_name,
                max_input_channels=max_in,
                default_samplerate_hz=_as_float_or_none(raw.get("default_samplerate")),
            )
        )
    return devices


def resolve_input_device_index(config: Dict[str, Any], logger: Any = None) -> Optional[int]:
    """Resolve an input device index from config.

    Priority:
      1) audio.device_index
      2) audio.device_id exact/substring match
      3) audio.device_selector.match_substring + require_input_channels
      4) best device with >= required channels
    """

    audio_cfg = config.get("audio", {})
    if not isinstance(audio_cfg, dict):
        audio_cfg = {}
    if "device_index" in audio_cfg and audio_cfg.get("device_index") is not None:
        candidate = _coerce_device_index(audio_cfg.get("device_index"))
        if candidate is not None:
            return candidate
        _log(
            logger,
            "warning",
            "audio.devices",
            "invalid_device_index",
            {"device_index": audio_cfg.get("device_index")},
        )

    required_channels = int(audio_cfg.get("channels", 0) or 0)
    device_id = audio_cfg.get("device_id")
    selector = audio_cfg.get("device_selector", {})
    if not isinstance(selector, dict):
        selector = {}
    match_substring = selector.get("match_substring")
    selector_required = int(selector.get("require_input_channels", required_channels) or 0)

    devices = list_input_devices()
    if not devices:
        _log(logger, "error", "audio.devices", "device_not_found", {"criteria": "sounddevice_missing_or_no_inputs"})
        return None

    # 2) device_id
    if device_id is not None:
        target = str(device_id).strip()
        if target:
            exact = [d for d in devices if d.name == target]
            if exact:
                chosen = _best_by_channels(exact, required_channels)
                _log(logger, "info", "audio.devices", "device_selected", {"device": asdict(chosen)})
                return chosen.index
            partial = [d for d in devices if target.lower() in d.name.lower()]
            if partial:
                chosen = _best_by_channels(partial, required_channels)
                _log(logger, "info", "audio.devices", "device_selected", {"device": asdict(chosen)})
                return chosen.index

    # 3) selector substring
    if match_substring is not None:
        target = str(match_substring).strip().lower()
        if target:
            matching = [d for d in devices if target in d.name.lower()]
            if matching:
                chosen = _best_by_channels(matching, selector_required)
                _log(logger, "info", "audio.devices", "device_selected", {"device": asdict(chosen)})
                return chosen.index

    # 4) default pick
    chosen = _best_by_channels(devices, required_channels)
    if chosen is None:
        _log(
            logger,
            "error",
            "audio.devices",
            "device_not_found",
            {"criteria": {"required_channels": required_channels, "devices": [asdict(d) for d in devices]}},
        )
        return None
    _log(logger, "info", "audio.devices", "device_selected", {"device": asdict(chosen)})
    return chosen.index


def _coerce_device_index(value: object) -> Optional[int]:
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return None
    return idx if idx >= 0 else None


def _best_by_channels(devices: List[AudioDeviceInfo], required_channels: int) -> Optional[AudioDeviceInfo]:
    if not devices:
        return None
    if required_channels <= 0:
        # Pick device with the most channels.
        return max(devices, key=lambda d: d.max_input_channels)
    eligible = [d for d in devices if d.max_input_channels >= required_channels]
    if eligible:
        return max(eligible, key=lambda d: d.max_input_channels)
    return None


def _as_float_or_none(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def _log(logger: Any, level: str, module: str, event: str, payload: Dict[str, Any]) -> None:
    if logger is None:
        return
    try:
        logger.emit(level, module, event, payload)
    except Exception:  # noqa: BLE001
        return

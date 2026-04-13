"""focusfield.audio.output.sink

CONTRACT: inline (source: src/focusfield/audio/output/sink.md)
ROLE: Output sink abstraction.

INPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - output.sink: file|host_loopback|usb_mic

PERF / TIMING:
  - real-time output
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from focusfield.audio.output.file_sink import start_file_sink
from focusfield.audio.output.rtp_pcm import start_rtp_pcm_sink
from focusfield.audio.output.virtual_mic import start_host_loopback_sink, start_usb_mic_sink, start_virtual_mic_sink


def start_output_sink(
    bus: Any,
    config: Dict[str, Any],
    logger: Any,
    stop_event: threading.Event,
) -> Optional[threading.Thread]:
    sink = str(config.get("output", {}).get("sink", ""))

    trace_cfg = config.get("trace", {})
    if not isinstance(trace_cfg, dict):
        trace_cfg = {}
    trace_enabled = bool(trace_cfg.get("enabled", True))
    run_dir = config.get("runtime", {}).get("artifacts", {}).get("dir_run")
    normalized_sink = sink.lower()
    if normalized_sink in {"file", "file_sink"}:
        # Avoid double-writing WAV files when trace recorder is active.
        if trace_enabled and run_dir:
            logger.emit("info", "audio.output.sink", "sink_skipped", {"sink": sink, "reason": "trace_recorder_active"})
            return None
        return start_file_sink(bus, config, logger, stop_event)
    if normalized_sink in {"host_loopback", "virtual_mic", "virtual"}:
        if normalized_sink in {"virtual_mic", "virtual"}:
            logger.emit("warning", "audio.output.sink", "sink_alias_used", {"alias": sink, "preferred": "host_loopback"})
            return start_virtual_mic_sink(bus, config, logger, stop_event)
        return start_host_loopback_sink(bus, config, logger, stop_event)
    if normalized_sink in {"usb_mic", "usb"}:
        return start_usb_mic_sink(bus, config, logger, stop_event)
    if normalized_sink in {"rtp_pcm", "rtp"}:
        return start_rtp_pcm_sink(bus, config, logger, stop_event)
    return None

"""focusfield.audio.output.sink

CONTRACT: inline (source: src/focusfield/audio/output/sink.md)
ROLE: Output sink abstraction.

INPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - output.sink: file|virtual_mic

PERF / TIMING:
  - real-time output
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from focusfield.audio.output.file_sink import start_file_sink


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
    if sink.lower() in {"file", "file_sink"}:
        # Avoid double-writing WAV files when trace recorder is active.
        if trace_enabled and run_dir:
            logger.emit("info", "audio.output.sink", "sink_skipped", {"sink": sink, "reason": "trace_recorder_active"})
            return None
        return start_file_sink(bus, config, logger, stop_event)
    if sink.lower() in {"virtual_mic", "virtual"}:
        logger.emit("warning", "audio.output.sink", "sink_error", {"sink": sink, "error": "virtual_mic_not_implemented"})
        return None
    return None

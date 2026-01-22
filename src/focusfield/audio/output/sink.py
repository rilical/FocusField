"""
CONTRACT: inline (source: src/focusfield/audio/output/sink.md)
ROLE: Output sink abstraction.

INPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - output.sink: virtual_mic|file|system
  - output.device: optional device name

PERF / TIMING:
  - real-time output

FAILURE MODES:
  - sink error -> log sink_error

LOG EVENTS:
  - module=audio.output.sink, event=sink_error, payload keys=sink, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/output/sink.md):
# Output sink contract

- Output EnhancedAudio to a sink.
- Sinks include virtual mic and file sink.
- Emit stats and dropout counts.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

"""
CONTRACT: inline (source: src/focusfield/audio/output/file_sink.md)
ROLE: File sink for enhanced audio.

INPUTS:
  - Topic: audio.enhanced.final  Type: EnhancedAudio
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - output.file_sink.path: output directory
  - output.file_sink.format: wav|flac

PERF / TIMING:
  - stream to disk without blocking

FAILURE MODES:
  - write error -> log write_failed

LOG EVENTS:
  - module=audio.output.file_sink, event=write_failed, payload keys=path, error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/output/file_sink.md):
# File sink

- WAV or FLAC logging spec.
- Include timestamps and metadata.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

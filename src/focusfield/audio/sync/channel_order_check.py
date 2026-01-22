"""
CONTRACT: inline (source: src/focusfield/audio/sync/channel_order_check.md)
ROLE: Verify channel mapping using test clip or live procedure.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - audio.sync.test_clip_path: path to test clip

PERF / TIMING:
  - offline calibration step

FAILURE MODES:
  - mapping mismatch -> log channel_order_mismatch

LOG EVENTS:
  - module=audio.channel_order_check, event=channel_order_mismatch, payload keys=expected, observed

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/audio/sync/channel_order_check.md):
# Channel order check

- Verify channel mapping using a test clip.
- Provide pass/fail criteria and logging.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

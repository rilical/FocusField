"""
CONTRACT: inline (source: src/focusfield/core/bus.md)
ROLE: In-process pub/sub with bounded queues.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - bus.max_queue_depth: per-topic queue depth

PERF / TIMING:
  - preserve per-topic ordering

FAILURE MODES:
  - queue full -> drop or backpressure -> log queue_full

LOG EVENTS:
  - module=core.bus, event=queue_full, payload keys=topic, depth

TESTS:
  - tests/contract_tests.md must cover backpressure rules

CONTRACT DETAILS (inline from src/focusfield/core/bus.md):
# Bus contract

- Typed topics with schema validation.
- Backpressure via bounded queues per topic.
- Publish/subscribe is non-blocking where possible.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

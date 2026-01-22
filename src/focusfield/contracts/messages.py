"""
CONTRACT: contracts/messages.md
ROLE: Typed message model placeholders (schemas are authoritative).

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - n/a

PERF / TIMING:
  - n/a

FAILURE MODES:
  - n/a

LOG EVENTS:
  - module=focusfield.contracts.messages, event=not_implemented, payload keys=reason

TESTS:
  - tests/contract_tests.md must cover schema validation
"""


def not_implemented() -> None:
    """Placeholder for typed message models."""
    raise NotImplementedError("FocusField contracts are defined in JSON schemas.")

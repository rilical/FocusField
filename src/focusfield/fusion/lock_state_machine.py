"""
CONTRACT: inline (source: src/focusfield/fusion/lock_state_machine.md)
ROLE: Target lock state machine with hysteresis.

INPUTS:
  - Topic: fusion.candidates  Type: AssociationCandidate[]
OUTPUTS:
  - Topic: fusion.target_lock  Type: TargetLock

CONFIG KEYS:
  - fusion.acquire_threshold: acquire threshold
  - fusion.hold_ms: hold duration
  - fusion.handoff_min_ms: handoff minimum
  - fusion.drop_threshold: drop threshold

PERF / TIMING:
  - per update; deterministic transitions

FAILURE MODES:
  - invalid transition -> log invalid_transition

LOG EVENTS:
  - module=fusion.lock_state_machine, event=invalid_transition, payload keys=state, reason

TESTS:
  - tests/lock_logic_sanity.md must cover hysteresis and handoff timing

CONTRACT DETAILS (inline from src/focusfield/fusion/lock_state_machine.md):
# Lock state machine

## States

- NO_LOCK, ACQUIRE, LOCKED, HOLD, HANDOFF.

## Transitions

- Acquire when mouth activity or VAD indicates speech.
- Lock when AV association confidence >= acquire_threshold.
- Hold during short silences up to hold_ms.
- Handoff when a new candidate dominates for handoff_min_ms.

## Failure handling

- Vision fails: rely on audio confidence until timeout.
- Audio confidence drops: fall back to HOLD or NO_LOCK.
- Disagreement: reduce confidence and avoid rapid switching.

## Output

- TargetLock includes reason strings for debugging and demos.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

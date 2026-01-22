# System invariants

## Timestamps

- t_ns must be monotonic per stream.
- Audio/video alignment must never exceed max_skew_ms (configurable).

## Angle conventions

- All angles are global azimuth degrees in [0, 360).
- Camera yaw offsets apply before wrapping.

## Lock stability

- TargetLock must not switch targets more frequently than handoff_min_ms unless lock is lost.

## No mic suppression

- Beamformer must consume all channels.
- Any channel drop must be explicitly logged as degraded mode.

## Determinism for FocusBench replay

- Replay must yield the same TargetLock sequence given the same inputs and config (within defined tolerance).

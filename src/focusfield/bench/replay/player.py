"""
CONTRACT: inline (source: src/focusfield/bench/replay/player.md)
ROLE: Deterministic replay of recorded scenes.

INPUTS:
  - Topic: n/a  Type: n/a
OUTPUTS:
  - Topic: audio.frames  Type: AudioFrame
  - Topic: vision.face_tracks  Type: FaceTrack[]
  - Topic: audio.doa_heatmap  Type: DoaHeatmap
  - Topic: fusion.target_lock  Type: TargetLock
  - Topic: audio.enhanced.final  Type: EnhancedAudio

CONFIG KEYS:
  - bench.replay_speed: playback rate
  - bench.deterministic_mode: deterministic ordering

PERF / TIMING:
  - deterministic scheduling

FAILURE MODES:
  - missing files -> log replay_failed

LOG EVENTS:
  - module=bench.player, event=replay_failed, payload keys=path, error

TESTS:
  - tests/replay_determinism.md must cover determinism

CONTRACT DETAILS (inline from src/focusfield/bench/replay/player.md):
# Replay player

- Deterministic replay of recorded scenes.
- Maintain timing and sequence determinism.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

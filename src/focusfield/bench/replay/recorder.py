"""
CONTRACT: inline (source: src/focusfield/bench/replay/recorder.md)
ROLE: Record live pipeline topics into bench scene.

INPUTS:
  - Topic: audio.frames  Type: AudioFrame
  - Topic: audio.vad  Type: AudioVad
  - Topic: vision.face_tracks  Type: FaceTrack[]
  - Topic: audio.doa_heatmap  Type: DoaHeatmap
  - Topic: vision.speaker_heatmap  Type: DoaHeatmap
  - Topic: fusion.target_lock  Type: TargetLock
  - Topic: audio.enhanced.final  Type: EnhancedAudio
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - bench.record_dir: output directory
  - bench.record_enabled: enable recording

PERF / TIMING:
  - write streams without blocking pipeline

FAILURE MODES:
  - disk write error -> log record_failed

LOG EVENTS:
  - module=bench.recorder, event=record_failed, payload keys=path, error

TESTS:
  - tests/replay_determinism.md must cover record/replay consistency

CONTRACT DETAILS (inline from src/focusfield/bench/replay/recorder.md):
# Recorder

- Record AudioFrame and FaceTrack (or VideoFrame) for replay.
- Store configs and calibration artifacts alongside recordings.
- Report specs: audio_raw.wav, enhanced.wav, tracks.jsonl, doa.jsonl, visual_heatmap.jsonl, vad.jsonl, lock.jsonl, scene.json.
"""

def not_implemented() -> None:
    """Placeholder to be replaced by implementation."""
    raise NotImplementedError("FocusField module stub.")

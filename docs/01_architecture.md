# FocusField architecture

## System blocks

- Audio capture -> VAD -> DOA heatmap -> beamform -> output sink.
- Vision capture -> face tracking -> mouth activity -> bearing mapping.
- Fusion associates DOA peaks with faces and drives target lock.

## Hardware abstraction

- Hardware is selected via configs and adapters only.
- No code changes between MVP and Full builds.

## Data flow

- All modules publish to a typed bus with schema validation.
- UI consumes telemetry and renders tiles + heatmap.

## Pipeline wiring (topics)

- AudioCapture publishes audio.frames.
- VAD subscribes audio.frames -> publishes audio.vad.
- DOA subscribes audio.frames -> publishes audio.doa_heatmap.
- CameraCapture publishes vision.frames.cam*.
- FaceTrack subscribes vision.frames.* -> publishes vision.face_tracks (bearing_deg_global + mouth_activity).
- AVAssociation subscribes audio.doa_heatmap + vision.face_tracks -> publishes fusion.candidates.
- LockStateMachine subscribes fusion.candidates (+ audio.vad) -> publishes fusion.target_lock.
- Beamformer subscribes audio.frames + fusion.target_lock -> publishes audio.enhanced.beamformed.
- Denoise subscribes audio.enhanced.beamformed -> publishes audio.enhanced.final.
- OutputSink subscribes audio.enhanced.final -> outputs audio (virtual mic or file).
- UI subscribes audio.doa_heatmap + vision.face_tracks + fusion.target_lock -> renders live view.

## Modes

- MVP, full, bench replay, lab debug.

## FaceLandmarker model management

- The FaceLandmarker model is downloaded on first run to `~/.cache/focusfield/face_landmarker.task`.
- Override the model path with `vision.mouth.mesh_model_path` in your config.
- If download is blocked (offline or restricted), set `mesh_model_path` to a local file path.

## Audio VAD behavior

- VAD subscribes to `audio.frames` and emits `audio.vad` with `speech` and `confidence`.
- When `fusion.require_vad: true`, the lock state machine will not acquire/hold a target during VAD silence,
  unless a speaking face is already detected (visual speaking takes precedence).
- When `fusion.require_vad: false`, VAD is advisory and lock decisions are purely visual.

Recommended VAD settings:

- MacBook (built‑in mono mic): `audio.vad.mode: 1`, `audio.vad.min_speech_ratio: 0.2`.
- Raspberry Pi (array or USB mic): `audio.vad.mode: 2`, `audio.vad.min_speech_ratio: 0.3`.

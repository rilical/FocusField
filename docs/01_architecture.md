# FocusField architecture

## System blocks

- Audio capture -> DOA heatmap -> beamform -> output sink.
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
- DOA subscribes audio.frames -> publishes audio.doa_heatmap.
- CameraCapture publishes vision.frames.cam*.
- FaceTrack subscribes vision.frames.* -> publishes vision.face_tracks (bearing_deg_global + mouth_activity).
- AVAssociation subscribes audio.doa_heatmap + vision.face_tracks -> publishes fusion.candidates.
- LockStateMachine subscribes fusion.candidates -> publishes fusion.target_lock.
- Beamformer subscribes audio.frames + fusion.target_lock -> publishes audio.enhanced.beamformed.
- Denoise subscribes audio.enhanced.beamformed -> publishes audio.enhanced.final.
- OutputSink subscribes audio.enhanced.final -> outputs audio (virtual mic or file).
- UI subscribes audio.doa_heatmap + vision.face_tracks + fusion.target_lock -> renders live view.

## Modes

- MVP, full, bench replay, lab debug.

# Topics map (frozen wiring spec)

This table is the single source of truth for topic names and message types. Do not diverge.

| Topic | Type | Producers | Consumers | Notes |
| --- | --- | --- | --- | --- |
| audio.frames | AudioFrame | audio.capture | audio.doa.srp_phat, audio.beamform.delay_and_sum, bench.recorder | Raw multichannel audio frames |
| audio.doa_heatmap | DoaHeatmap | audio.doa.srp_phat | fusion.av_association, ui.telemetry, bench.recorder | 0..360 deg likelihood over azimuth |
| vision.frames.cam0 | VideoFrame | vision.cameras | vision.tracking.face_track | Camera 0 frames (internal) |
| vision.frames.cam1 | VideoFrame | vision.cameras | vision.tracking.face_track | Camera 1 frames (internal) |
| vision.frames.cam2 | VideoFrame | vision.cameras | vision.tracking.face_track | Camera 2 frames (internal) |
| vision.face_tracks | FaceTrack[] | vision.tracking.face_track | fusion.av_association, ui.telemetry, bench.recorder | Merged tracks from all cameras |
| fusion.candidates | AssociationCandidate[] | fusion.av_association | fusion.lock_state_machine, fusion.target_output | Internal association candidates |
| fusion.target_lock | TargetLock | fusion.lock_state_machine | audio.beamform.delay_and_sum, ui.telemetry, bench.recorder | Target lock state machine output |
| audio.enhanced.beamformed | EnhancedAudio | audio.beamform.delay_and_sum | audio.enhance.denoise, audio.output.sink, bench.recorder | Beamformed stream |
| audio.enhanced.final | EnhancedAudio | audio.enhance.denoise, audio.enhance.agc_post | audio.output.sink, bench.recorder | Final enhanced stream |
| ui.telemetry | TelemetrySnapshot | ui.telemetry | ui.server, ui.views.live | UI-only aggregated snapshot |
| log.events | LogEvent | all modules | core.logging, ui.telemetry | Structured log events |
| bench.record | Tap | bench.recorder | n/a | Recorder subscribes to key topics |
| bench.report | BenchReport | bench.focusbench | ui.views.bench, user | FocusBench report bundle |

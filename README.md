# FocusField

FocusField is a portable "acoustic lens" for calls. It identifies who is speaking (via mouth activity), localizes where the sound is coming from (via a 360 deg acoustic heatmap), locks onto the intended talker, beamforms to suppress off-axis talkers/noise, and outputs a single "clean microphone" stream suitable for conferencing apps. No panorama stitching is required; the UI renders camera tiles plus a polar acoustic heatmap.

FocusField is built to scale by configuration:

MVP: 1 camera + 4 microphones (fast to ship)

Full: 3 cameras + 8 microphones (360 deg coverage, stronger suppression)

What FocusField is not:

It does not perform open-air room noise cancellation.

It does not guarantee perfect separation of multiple simultaneous speakers.

## Quick glossary (plain English)

If you're new to audio and signal processing, this is the minimum you need to know:

- WAV: a simple, uncompressed audio file format. We use it for debugging because it's "raw" and faithful.
- DOA (Direction of Arrival): the direction the sound is coming from (0-360 degrees around the device).
- DOA peak: the strongest direction in the heatmap. This is where we point the beam.
- SRP-PHAT: a robust method to estimate DOA using multiple microphones (compares time delays).
- GCC-PHAT: a simpler baseline DOA method (also uses time delay between mics).
- VAD (Voice Activity Detection): a yes/no signal that says "someone is speaking right now."

Why it matters:

- DOA + peak tells us where to aim the audio focus.
- Beamforming uses that direction to amplify the target voice.
- VAD prevents the system from locking or steering when the room is silent.

## System overview

```mermaid
flowchart LR
  subgraph Sensors
    A["Mic Array (4ch or 8ch)"] --> B[Audio Capture]
    G["Camera(s) (1 or 3)"] --> H[Video Capture]
  end

  subgraph Audio
    B --> C["DOA Heatmap (SRP-PHAT over 0..360)"]
    B --> VAD["VAD (WebRTC)"]
    B --> D["Beamformer (Delay-and-Sum, MVP)"]
    D --> E["Denoise (optional)"]
  end

  subgraph Vision
    H --> V1[Face Track]
    V1 --> V2["Mouth Activity Score (FaceLandmarker)"]
    V2 --> V3["Face Bearing -> Global Azimuth"]
  end

  subgraph Fusion
    C --> F1["AV Association (DOA peaks <-> faces)"]
    V3 --> F1
  VAD --> F2
  F1 --> F2["Target Lock State Machine (Acquire / Lock / Hold / Handoff)"]
    F2 --> D
  end

  E --> OUT["Output Sink (Virtual Mic / File Sink)"]
  C --> UI["Web UI (Heatmap + Tiles + Logs)"]
  V3 --> UI
  F2 --> UI
  F2 --> LOG["Structured Logs + FocusBench Artifacts"]
  C --> LOG
```

## Acoustic heatmap definition

The 360 deg heatmap is computed from multichannel audio using SRP-PHAT (scores over azimuth bins). Vision does not generate the acoustic heatmap; vision provides a speaker prior (mouth activity) used to select and stabilize the beam steering target.

## Data contracts

All modules communicate using standardized message types: AudioFrame, AudioVad, VideoFrame, FaceTrack, DoaHeatmap, TargetLock, EnhancedAudio, LogEvent. These contracts are the single source of truth:

- contracts/messages.md
- contracts/json_schemas/...
- contracts/invariants.md

## Target lock logic (what makes FocusField usable)

FocusField does not chase the loudest sound blindly. It locks onto a target using mouth activity + audio DOA agreement and applies hysteresis to prevent jitter.

```mermaid
stateDiagram-v2
  [*] --> NO_LOCK
  NO_LOCK --> ACQUIRE: mouth_activity OR VAD indicates speech
  ACQUIRE --> LOCKED: AV association confidence >= acquire_threshold
  LOCKED --> HOLD: short silence (<= hold_ms)
  HOLD --> LOCKED: resumes quickly
  HOLD --> NO_LOCK: timeout / lost face and low audio confidence
  LOCKED --> HANDOFF: new candidate dominates for handoff_min_ms
  HANDOFF --> LOCKED: commit new target
  LOCKED --> NO_LOCK: tracking lost / confidence collapse
```

## MVP vs Full builds

MVP (1 cam + 4 mic): validates end-to-end focusing, UI, and quantitative metrics in a controlled setup.
Full (3 cam + 8 mic): upgrades coverage and suppression; the pipeline stays unchanged (config + hardware adapters only).

```mermaid
flowchart TB
  subgraph MVP
    m1[1 camera] --> m2[vision]
    a1[4 mics] --> a2[audio]
    m2 --> f[fusion] --> b[beamform] --> o[output]
    a2 --> f
  end

  subgraph FULL
    c1[3 cameras @ 0/120/240 deg] --> v[vision]
    u1[8 mics] --> a[audio]
    v --> f2[fusion] --> b2[beamform] --> o2[output]
    a --> f2
  end
```

## Calibration and repeatability

FocusField includes explicit calibration artifacts so results are reproducible:

- Camera bearing calibration (pixel->bearing->global azimuth)
- Mic array geometry + channel-order verification
- Time sync sanity checks (audio/video skew bounds)

## Run artifacts, tracing, and debugging (first-class)

Every run produces a **self-contained debug bundle** under:

`artifacts/<run_id>/`

This is the fastest way to debug on-device (Pi) without guesswork. If something is wrong, you can zip that folder and hand it to another engineer.

Key files:

- `run_meta.json`: device selection + versions + config snapshot
- `config_effective.yaml`: merged config actually used
- `logs/events.jsonl`: structured logs (one JSON per line)
- `logs/perf.jsonl`: lightweight perf/latency snapshots
- `traces/*.jsonl`: topic traces (VAD/DOA/faces/lock/beamformer debug)
- `audio/enhanced.wav`: mono enhanced output
- `audio/raw.wav`: raw multichannel capture (optional)
- `thumbs/*.jpg`: 1fps thumbnails per camera (optional)
- `crash/crash.json`: written only on crash (traceback + last known state)

Important behavior:

- When `trace.enabled: true`, the trace recorder is the **canonical WAV writer**. The legacy file sink is skipped to avoid double-writing.
- To disable all tracing/artifacts (and use the file sink only), set `trace.enabled: false`.

Pi setup + bring-up instructions live in `docs/30_pi_runbook.md`.

## Virtual mic output (macOS)

To route FocusField’s enhanced mono stream into call apps (Zoom/Meet/etc.) on macOS using a loopback device (BlackHole/Loopback), see:

- `docs/virtual_mic_mac.md`

## FocusBench (quantitative evaluation and regression)

FocusBench replays recorded scenes (audio and optional video), runs the identical pipeline deterministically, and produces a report bundle with plots and metrics (MAE, delta SIR, WER, latency histograms). This prevents "it sounded better bro."

```mermaid
flowchart LR
  S[Scene Manifest] --> P[Replay Player]
  P --> PIPE[Pipeline]
  PIPE --> M[Metrics]
  M --> R[BenchReport.json]
  M --> PLOTS[Required Plots]
  R --> DIFF["Regression Compare (runA vs runB)"]
```

## Required metrics (minimum reporting set)

- Steering MAE (deg) vs angle and environment
- Target selection accuracy (%) in 2-person tests
- Interferer suppression delta SIR (dB) vs interferer angle
- Intelligibility proxy: ASR WER reduction (%)
- Added latency (ms) + dropouts (count)

Definitions and module contracts are embedded in `src/focusfield/**.py` docstrings and summarized in `docs/11_contract_index.md`.

## Deliverables

- A portable sensor pod + pipeline producing a focused "clean mic" stream for calls
- UI showing camera tiles, 360 deg heatmap, and lock status (with active-speaker highlight)
- FocusBench reports enabling regression testing and quantitative claims

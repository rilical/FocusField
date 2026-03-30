# FocusField Pi4 Runbook (A→Z)

This runbook provides two explicit run modes on Raspberry Pi 4/5:
- strict full-target mode (3 capture cameras + 8 mic channels required)
- degraded debug mode (best-effort bring-up for diagnostics)

## Hardware checklist (avoid random failures)

- Use a **powered USB 3.0 hub** for 3× UVC cameras + UMA-8.
- Prefer **MJPEG** at **640×360 @ 15fps** for each camera.
- Plug the hub into a Pi USB3 port (blue).

## OS / Packages

### Raspberry Pi OS (64-bit)

```bash
set -euo pipefail
cd /home/focus/FocusField
git checkout main
git pull --ff-only
sudo apt update
sudo apt install -y \
  python3-pip python3-venv \
  portaudio19-dev \
  v4l-utils \
  libatlas3-base \
  libopenblas-dev \
  ffmpeg \
  python3-opencv
```

## Python environment

```bash
set -euo pipefail
deactivate 2>/dev/null || true
rm -rf .venv
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -U pip
pip install --no-deps -e .
pip install -U "PyYAML>=6.0" "numpy>=1.23" "sounddevice>=0.4.6" "webrtcvad>=2.0.10"
```

> FocusField on Raspberry Pi uses Debian's `python3-opencv` for `cv2`. `opencv-python`
> wheels are not published for this environment, and `mediapipe` is not currently available
> on ARM64 Python 3.13 wheels in this OS image.

## Stable camera paths

### Hardware verification (required before strict full-target)

```bash
v4l2-ctl --list-devices
ls -l /dev/v4l/by-path /dev/v4l/by-id
lsusb -t
```

If you do not see at least three capture-capable camera endpoints and one input
audio device with at least 8 channels, strict full-target mode will fail fast.

### UMA-8 mode gate (mandatory before strict full-target)

Run this and verify the raw array endpoint exposes 8 input channels:

```bash
python3 - <<'PY'
import sounddevice as sd
for i, d in enumerate(sd.query_devices()):
    if int(d.get("max_input_channels") or 0) > 0:
        print(i, d.get("name"), int(d.get("max_input_channels") or 0))
PY
```

Pass condition:
- At least one raw-array input device reports `max_input_channels >= 8`.
- Common names include `miniDSP` and `micArray RAW SPK`.

If the raw-array device only reports 2 channels, it is in DSP mode and strict full-target will fail.
Switch UMA-8 to RAW firmware first.

### Meeting-peripheral production path (recommended)

Use this for the shipping appliance path where FocusField should come up audio-first
and present a host-facing meeting microphone as soon as possible:

- `configs/meeting_peripheral.yaml`

Provision and validate the production path with the guided workflow:

```bash
set -euo pipefail
python3 scripts/provision_focusfield.py \
  --config configs/meeting_peripheral.yaml \
  --json

python3 -m focusfield.main.run --config configs/meeting_peripheral.yaml
```

Install the service with the meeting-peripheral config:

```bash
cd /home/focus/FocusField
sudo scripts/install_systemd_service.sh focusfield /home/focus/FocusField/configs/meeting_peripheral.yaml
sudo systemctl enable --now focusfield
```

Use `configs/full_3cam_8mic_pi*.yaml` only for strict bring-up, diagnostics, and
bench/perf work.

### Strict full-target config generation (bring-up and diagnostics)

Use this base config for Pi bring-up, root-cause work, and the stable local service:
- `configs/full_3cam_8mic_pi.yaml`

Keep this only for the tighter threaded production operating point after the rig is already stable:
- `configs/full_3cam_8mic_pi_prod.yaml`

Keep these only for debug or bench work:
- `configs/full_3cam_8mic_pi_hq_aggressive.yaml`
- `configs/full_3cam_8mic_pi_hq_balanced.yaml`

```bash
set -euo pipefail
python3 scripts/prepare_pi_local_config.py \
  --base-config configs/full_3cam_8mic_pi.yaml \
  --output configs/full_3cam_working_local.yaml \
  --camera-source by-path \
  --camera-scope usb \
  --max-cameras 3 \
  --require-cameras 3 \
  --require-audio-channels 8 \
  --strict && \
python3 scripts/pi_preflight.py \
  --config configs/full_3cam_working_local.yaml \
  --camera-source by-path \
  --camera-scope usb \
  --require-cameras 3 \
  --require-audio-channels 8 \
  --strict \
  --require-led-hid && \
python3 scripts/list_cameras.py
```

Then verify:

```bash
python3 -m focusfield.main.run --config configs/full_3cam_working_local.yaml
```

### Camera + UMA-8 directional alignment

Use this once your 3-camera rig is physically mounted.

1. Set physical reference direction:
- Define cam0/front as global 0deg.
- Mount cam1 approximately +120deg and cam2 approximately +240deg around the UMA-8.

2. Confirm config azimuths:
- `video.cameras[0].yaw_offset_deg: 0`
- `video.cameras[1].yaw_offset_deg: 120`
- `video.cameras[2].yaw_offset_deg: 240`

3. Enable LED simulation/hardware output in config:
- `uma8_leds.enabled: true`
- `uma8_leds.backend: simulate` first, then `hid` when protocol path is confirmed.

4. Run preflight and smoke:

```bash
python3 scripts/pi_preflight.py \
  --config configs/full_3cam_working_local.yaml \
  --camera-source by-path \
  --camera-scope usb \
  --require-cameras 3 \
  --require-audio-channels 8 \
  --strict

python3 scripts/pi_smoke.py \
  --config configs/full_3cam_working_local.yaml \
  --run-seconds 30 \
  --strict \
  --camera-scope usb
```

5. Verify in browser:
- Open `http://<pi-ip>:8080/telemetry`
- Check `lock_state.target_bearing_deg` and `uma8_leds.sector` while speaking from known angles.

6. Calibrate mic yaw before per-camera tuning:

```bash
sudo systemctl stop focusfield || true
pkill -f 'python3 -m focusfield.main.run' || true
python3 scripts/calibrate_uma8.py --device "micArray RAW SPK"
```

- Place the speaker directly in front of cam0 during the orientation step.
- Update `configs/device_profiles.yaml` with the emitted `yaw_offset_deg` and `channel_order`.

7. Tune final alignment:
- Rotate LED ring mapping with `uma8_leds.base_bearing_offset_deg`.
- If audio direction is correct but face direction is off, tune each `video.cameras[].yaw_offset_deg`.

### Debug/degraded config generation (best-effort diagnostics)

Use this when strict contract cannot be met and you still need traces/logs:

```bash
set -euo pipefail
python3 scripts/prepare_pi_local_config.py \
  --base-config configs/full_3cam_8mic_pi_debug.yaml \
  --output configs/full_3cam_working_local.yaml \
  --camera-source auto \
  --camera-scope any \
  --max-cameras 3 && \
python3 scripts/pi_preflight.py --config configs/full_3cam_working_local.yaml --camera-source auto
```

## UMA-8 calibration

Channel-order and yaw alignment are critical for DOA/beamforming.

```bash
sudo systemctl stop focusfield || true
pkill -f 'python3 -m focusfield.main.run' || true
python3 scripts/calibrate_uma8.py --device "micArray RAW SPK"
```

Paste the emitted YAML snippet into `configs/device_profiles.yaml`.

## Smoke test (sensors + pipeline)

```bash
python3 scripts/pi_smoke.py --config configs/full_3cam_working_local.yaml --run-seconds 10
```

If you see:

```text
cascade_missing
```

your OpenCV install is missing Haar cascades. Face tracking is disabled by design and the pipeline continues.
To re-enable face detection, install the distro OpenCV data package and rerun:

```bash
sudo apt install -y opencv-data libopencv-data || sudo apt install -y opencv-data
python3 -m pip install --no-deps -e .
```

If strict full-target is required, do not proceed when smoke/preflight reports fewer than
3 openable USB cameras or fewer than 8 audio channels.

## Running live

### Strict full-target run

```bash
python3 -m focusfield.main.run --config configs/full_3cam_working_local.yaml
```

### Auto-start at boot (Raspberry Pi)

Once the target config is valid on the device, install a systemd service:

```bash
cd /home/focus/FocusField
sudo scripts/install_systemd_service.sh focusfield /home/focus/FocusField/configs/meeting_peripheral.yaml
sudo systemctl enable --now focusfield
```

The installer sets a boot-time wrapper that:

- validates bundled model assets before installation when runtime downloads are disabled,
- waits for USB/video to settle only for full vision modes,
- runs `scripts/pi_preflight.py` up to 15 times before giving up,
- starts `focusfield.main.run` using the mode already declared in the config,
- auto-restarts the service if it exits.

For `meeting_peripheral` and `appliance_fastboot`, boot is audio-first: the service starts without waiting on cameras, and the preflight step skips vision readiness so the host can see a microphone immediately.

Useful checks:

```bash
sudo systemctl status focusfield
sudo journalctl -u focusfield -f
```

Expected UMA-8 LED events in logs when enabled:
- `uma8_leds.started`
- `uma8_leds.transport_init_ok`
- `uma8_leds.frame_sent`

To tune startup behavior, set these env vars before install so they are written into the service:

- `FOCUSFIELD_PRECHECK_RETRIES` (default `15`)
- `FOCUSFIELD_PRECHECK_DELAY_SECONDS` (default `5`)
- `FOCUSFIELD_CAMERA_SOURCE` (default `by-path`)
- `FOCUSFIELD_CAMERA_SCOPE` (default `usb`)
- `FOCUSFIELD_ENABLE_UMA8_LEDS` (`true`/`false`, optional runtime override)
- `OMP_NUM_THREADS` (default `1`, recommended on Pi)
- `OPENBLAS_NUM_THREADS` (default `1`, recommended on Pi)
- `MKL_NUM_THREADS` (default `1`, recommended on Pi)

Example:

```bash
FOCUSFIELD_CAMERA_SOURCE=auto \
FOCUSFIELD_CAMERA_SCOPE=any \
FOCUSFIELD_PRECHECK_RETRIES=30 \
FOCUSFIELD_PRECHECK_DELAY_SECONDS=3 \
FOCUSFIELD_ENABLE_UMA8_LEDS=true \
OMP_NUM_THREADS=1 \
OPENBLAS_NUM_THREADS=1 \
MKL_NUM_THREADS=1 \
sudo scripts/install_systemd_service.sh focusfield /home/focus/FocusField/configs/full_3cam_working_local.yaml
```

For latency-first Raspberry Pi operation, set `runtime.perf_profile: realtime_pi_max` in your active config.

If strict boot keeps failing, inspect logs and confirm camera/audio contracts first:

- run `python3 scripts/list_cameras.py`
- run `python3 scripts/pi_preflight.py --config ... --camera-scope usb --require-cameras 3 --require-audio-channels 8 --strict`

## Mandatory A/B benchmark gate (release flow)

Run this after capturing:
- one baseline run in UMA-8 DSP mode
- one candidate run in UMA-8 RAW + FocusField mode

```bash
set -euo pipefail
python3 scripts/focusbench_ab.py \
  --baseline-run /path/to/artifacts/<baseline_run_id> \
  --candidate-run /path/to/artifacts/<candidate_run_id> \
  --scene-manifest bench_scenes/quiet_office.yaml \
  --config configs/full_3cam_8mic_pi.yaml \
  --output-dir artifacts/focusbench_ab/<candidate_run_id>
```

Expected:
- exit code `0` means all release gates passed
- exit code `2` means one or more quality/runtime gates failed
- report at `artifacts/focusbench_ab/<candidate_run_id>/BenchReport.json`

Recommended: repeat the same command for each standard manifest in `bench_scenes/`.

Production calibration checklist:
- verify camera order after reboot
- confirm camera paths resolve by `by-path`
- confirm UMA-8 RAW mode exposes at least 8 input channels
- run UMA-8 calibration
- confirm `yaw_offset_deg`
- confirm LED ring bearing mapping
- confirm lock bearing against known speaker positions at `0`, `120`, and `240` degrees

## Demo-safe Zoom workflow

Use the dedicated demo profile when the goal is a reliable live Zoom demo plus
engineering-grade evidence capture:

```bash
python3 -m focusfield.main.run \
  --config configs/meeting_peripheral_demo_safe.yaml \
  --mode meeting_peripheral
```

Behavioral intent of this profile:

- stable switching over aggressive handoff
- `require_vad: true` and `require_speaking: true`
- audio-only fallback when vision confidence is weak
- tracing enabled for later benchmark and incident review
- UI disabled to preserve demo stability

Target environment for claims:

- one validated noisier office room
- fixed 3-camera + 8-mic rig geometry
- seated participants
- normal meeting distances

### Demo rehearsal gate

After a 30-minute rehearsal run and a Zoom evidence capture:

```bash
python3 scripts/demo_rehearsal_gate.py \
  --config configs/meeting_peripheral_demo_safe.yaml \
  --run-dir artifacts/LATEST \
  --host-gate-evidence artifacts/demo/zoom_host_gate.json \
  --output artifacts/demo/demo_readiness.json
```

Expected outputs in `demo_readiness.json`:

- boot to host-visible mic
- Zoom device-selection proof
- reconnect time
- latency p50/p95/p99
- output underrun totals/rate
- queue pressure peak
- crash-free soak verdict

### Same-session A/B benchmark assembly

Capture the FocusField candidate run, the MacBook built-in mic WAV, and the
close-talk reference WAV from the same session. Then assemble the benchmark:

```bash
python3 scripts/demo_ab_capture.py \
  --candidate-run artifacts/LATEST \
  --baseline-audio /path/to/macbook_built_in.wav \
  --reference-audio /path/to/close_talk_reference.wav \
  --scene-spec /path/to/demo_scenes.yaml \
  --output-dir artifacts/demo/ab_bundle \
  --video-path /path/to/cam0.mp4 \
  --video-path /path/to/cam1.mp4 \
  --video-path /path/to/cam2.mp4
```

This writes:

- `capture_bundle.json`
- `scene_manifest.yaml`
- `scene_timing_metadata.json`

Use the generated manifest with FocusBench:

```bash
python3 scripts/focusbench_ab.py \
  --baseline-run artifacts/LATEST \
  --candidate-run artifacts/LATEST \
  --scene-manifest artifacts/demo/ab_bundle/scene_manifest.yaml \
  --config configs/meeting_peripheral_demo_safe.yaml \
  --output-dir artifacts/demo/focusbench
```

For same-session A/B runs, the manifest carries explicit
`baseline_audio_path`, `candidate_audio_path`, and `reference_audio_path`
overrides, so the benchmark can score the MacBook baseline against the
FocusField candidate while still reusing candidate runtime traces.

### Engineering panel packet

Generate a concise scorecard and collect the plots into one folder:

```bash
python3 scripts/demo_panel_report.py \
  --bench-report artifacts/demo/focusbench/BenchReport.json \
  --demo-readiness artifacts/demo/demo_readiness.json \
  --output-dir artifacts/demo/panel_packet
```

The panel packet should show:

- clarity deltas: `SI-SDR`, `STOI`, `WER relative improvement`, `SIR`
- runtime: latency p50/p95/p99
- stability: output underrun rate and queue pressure
- meeting path: boot-to-host-visible mic and reconnect time

### Claim language

Allowed claim language:

- "In this validated noisy office room, FocusField produced clearer speech than the MacBook built-in microphone."
- "These latency values are internal pipeline measurements."
- "Boot-to-host-visible mic and reconnect timings were measured on the demo appliance."

Do not claim:

- broad room portability
- network latency
- end-to-end meeting round-trip latency

### Degraded debug run

```bash
python3 -m focusfield.main.run --config configs/full_3cam_working_local.yaml
```

Open the UI:

- `http://<pi-ip>:8080/`

## Artifacts / Debug bundles

Each run creates:

`artifacts/<run_id>/`

- `run_meta.json` + `config_effective.yaml`
- `logs/events.jsonl` + `logs/perf.jsonl`
- `audio/enhanced.wav` (+ `audio/raw.wav` if enabled)
- `traces/*.jsonl`
- `thumbs/*.jpg` (1fps thumbnails)
- `crash/crash.json` (only on crash)

Generate a portable support bundle instead of manually zipping files:

```bash
python3 scripts/support_bundle.py \
  --config configs/meeting_peripheral.yaml \
  --run-dir artifacts/LATEST \
  --output focusfield_support_bundle.zip
```

Useful guided workflows:

```bash
python3 scripts/calibration_workflow.py --config configs/meeting_peripheral.yaml
python3 scripts/recover_focusfield.py --config configs/meeting_peripheral.yaml --run-dir artifacts/LATEST
```

## Common failure modes

### Cameras reshuffle

- Prefer `/dev/v4l/by-path` for strict mode to bind identity to physical USB ports.
- Strict mode counts only USB camera nodes (`camera_scope=usb`) toward the 3-camera contract.
- Use `/dev/v4l/by-id` or numeric indices only for debug mode.

### USB bandwidth issues

- Force MJPEG.
- Reduce fps to 10–15.
- Ensure powered hub.

### Audio underruns

- Increase `audio.block_size` to 1024 or 2048.
- Lower camera fps.
- Check for thermal throttling.

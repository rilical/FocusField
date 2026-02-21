# FocusField Pi4 Runbook (A→Z)

This runbook is the fastest path to a stable bring-up on Raspberry Pi 4/5.

## Hardware checklist (avoid random failures)

- Use a **powered USB 3.0 hub** for 3× UVC cameras + UMA-8.
- Prefer **MJPEG** at **640×360 @ 15fps** for each camera.
- Plug the hub into a Pi USB3 port (blue).

## OS / Packages

### Raspberry Pi OS (64-bit)

```bash
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

Prefer `/dev/v4l/by-id/*` symlinks (stable across reboots):

```bash
python3 scripts/list_cameras.py
```

Copy the desired paths into your config under `video.cameras[].device_path`.

## UMA-8 calibration

Channel-order and yaw alignment are critical for DOA/beamforming.

```bash
python3 scripts/calibrate_uma8.py --device miniDSP
```

Paste the emitted YAML snippet into `configs/device_profiles.yaml`.

## Smoke test (sensors + pipeline)

```bash
python3 scripts/pi_smoke.py --config configs/full_3cam_8mic_pi.yaml --run-seconds 10
```

## Running live

```bash
python3 -m focusfield.main.run --config configs/full_3cam_8mic_pi.yaml
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

If something goes wrong, zip the run folder and share it.

## Common failure modes

### Cameras reshuffle

- Use `/dev/v4l/by-id` paths (not numeric indices).

### USB bandwidth issues

- Force MJPEG.
- Reduce fps to 10–15.
- Ensure powered hub.

### Audio underruns

- Increase `audio.block_size` to 1024 or 2048.
- Lower camera fps.
- Check for thermal throttling.

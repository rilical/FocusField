# Virtual Mic (macOS)

FocusField can output `audio.enhanced.final` to a loopback audio device so conferencing apps (Zoom/Meet/etc.) can use it as a microphone.

This repo **does not** implement a kernel/driver-level virtual microphone. Instead, it writes to an existing loopback device using `sounddevice` (PortAudio).

## Recommended Loopback: BlackHole (2ch)

1. Install BlackHole 2ch.
2. In your FocusField config:

```yaml
output:
  sink: virtual_mic
  virtual_mic:
    channels: 2
    device_selector:
      match_substring: "BlackHole"
```

3. Run FocusField (example):

```bash
python3 -m focusfield.main.run --config configs/mvp_1cam_4mic.yaml
```

4. In Zoom/Meet/etc, select **BlackHole 2ch** (or your chosen loopback device) as the microphone input.

## Notes / Troubleshooting

- If the output device can’t be opened, FocusField logs `audio.output.virtual_mic.device_error`.
- If you configure `device_selector.match_substring` and no device matches, FocusField logs `audio.output.virtual_mic.device_not_found` and stops.
- If you see frequent `audio.output.virtual_mic.underrun`, increase CPU headroom and/or reduce frame rates, and consider lowering `ui.telemetry_hz`.

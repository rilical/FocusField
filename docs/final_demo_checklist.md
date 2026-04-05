# FocusField Final Demo Checklist

## Cable path

- Pi direct-demo connector: `usb-c-otg`
- Mac connector: direct USB-C input or trusted USB-A-to-USB-C data cable into the Mac
- Do not use a Pi host-only USB-A port for the Mac meeting path

## Appliance gate

- Run `python3 scripts/check_usb_demo_feasibility.py appliance --connector-port usb-c-otg --output artifacts/demo/appliance_usb_gate.json`
- Confirm the report passes
- If the report shows `connector_port_host_only` or `no_udc_controller`, stop the direct-cable demo path

## Service bring-up

- Install with `sudo FOCUSFIELD_USB_GADGET_CONNECTOR_PORT=usb-c-otg scripts/install_systemd_service.sh focusfield /home/focus/FocusField/configs/meeting_peripheral_demo_ui.yaml`
- Start `focusfield-usb-gadget.service`
- Start `focusfield.service`
- Confirm `FocusField USB Mic` is the gadget product name

## Mac verification

- Capture `macos_before.json` before attaching the cable
- Attach the cable
- Capture `macos_after.json`
- Run `python3 scripts/check_usb_demo_feasibility.py macos-compare --before artifacts/demo/macos_before.json --after artifacts/demo/macos_after.json --expected-device-name "FocusField USB Mic" --output artifacts/demo/zoom_host_gate.json`
- Confirm the report passes before the rehearsal

## Zoom input selection

- Start on the Mac built-in microphone
- Switch Zoom input to `FocusField USB Mic`
- Verify Zoom keeps the selection after a short pause
- If Zoom drops the selection after attach or replug, stop and fix before demo day

## UI

- UI URL: `http://<pi-ip>:8080/`
- Open exactly one browser client
- Show tracks, lock state, directionality, and runtime health
- Do not touch calibration controls during the demo

## A/B sequence

1. Start with the Mac built-in mic selected in Zoom.
2. Show the FocusField UI.
3. Switch Zoom to `FocusField USB Mic`.
4. Run target speech plus interference.
5. Switch back to the Mac built-in mic.
6. End with `FocusField USB Mic`.

## Reconnect fallback

- If the cable is replugged, wait for `FocusField USB Mic` to reappear
- Re-select `FocusField USB Mic` in Zoom if needed
- If reconnect takes longer than `10s`, treat the direct USB path as not demo-ready

## Final gate

- Cold boot with the Mac attached
- Host-visible mic appears within `30s`
- Reconnect recovery completes within `10s`
- 30-minute rehearsal completes crash-free
- `demo_rehearsal_gate` passes and writes `artifacts/demo/demo_readiness.json`

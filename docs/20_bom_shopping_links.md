# FocusField Full Build BOM + Shopping Links (3 cams / 8 mics)

Last updated: 2026-01-27

Goal: full 360 build with 3 cameras + UMA-8 mic array + Raspberry Pi 5.
Note: prices change often; treat totals as estimates before tax/shipping.

## Core Required Hardware

| Item | Qty | Unit Price (USD) | Subtotal | Link |
| --- | --- | --- | --- | --- |
| Raspberry Pi 5 (8GB) | 1 | 104.50 | 104.50 | https://www.adafruit.com/product/5813 |
| miniDSP UMA-8 USB mic array (8ch) | 1 | 105.00 | 105.00 | https://www.minidsp.com/products/usb-audio-interface/uma-8-microphone-array |
| Arducam IMX291 160 deg UVC USB camera | 3 | 52.99 | 158.97 | https://www.arducam.com/ub020201-arducam-1080p-low-light-wdr-usb-camera-module-with-metal-case-2mp-1-2-8-cmos-imx291-160-degree-ultra-wide-angle-mini-uvc-webcam-board-with-microphones.html |
| Powered USB 3.0 hub (7-port, 36W) | 1 | 39.95 | 39.95 | https://plugable.com/products/usb3-hub7c/ |

Core subtotal: 408.42

## Required for Stable Operation

| Item | Qty | Unit Price (USD) | Subtotal | Link |
| --- | --- | --- | --- | --- |
| Official Raspberry Pi 27W USB-C power supply | 1 | 14.04 | 14.04 | https://www.adafruit.com/product/5814 |
| Official Raspberry Pi 5 Active Cooler | 1 | 13.50 | 13.50 | https://www.adafruit.com/product/5815 |

Add-on subtotal: 27.54

## Optional (Recommended)

| Item | Qty | Unit Price (USD) | Subtotal | Link |
| --- | --- | --- | --- | --- |
| Official Raspberry Pi 5 Case | 1 | 10.95 | 10.95 | https://www.canakit.com/official-raspberry-pi-5-case.html |

Optional subtotal: 10.95

## Alternative Compute Kit (simplifies ordering)

If you want one SKU that already includes a case, PSU, and microSD card:

| Item | Qty | Unit Price (USD) | Link |
| --- | --- | --- | --- |
| Raspberry Pi 5 Budget Kit - 8GB (includes case + PSU + SD) | 1 | 120.95 | https://www.pishop.us/product/raspberry-pi-5-budget-kit-8gb/ |

## Estimated Totals (USD)

- Core subtotal: 408.42
- Core + power + cooling: 435.96
- Core + power + cooling + case: 446.91

## 3D Printed Dome (Fabrication Notes)

This is custom. Use the CAD folder in this repo as the starting point (or design in Fusion 360).

Recommended features:
- 3 camera mounts at 0 / 120 / 240 degrees.
- Center mount for UMA-8.
- Strain relief points for USB cables.
- Ventilation for Pi 5 + cooler.
- Marked 0 deg reference on the dome.

## Wiring / Integration (matches current codebase)

- USB hub to Raspberry Pi 5.
- UMA-8 to hub (appears as 8-channel UAC2 input).
- cam0/cam1/cam2 to hub (UVC devices).
- FocusField runs on the Pi and uses configs/full_3cam_8mic.yaml.


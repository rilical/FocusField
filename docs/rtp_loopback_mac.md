# RTP Loopback Mic

Use this path when FocusField runs on the Pi and your Mac should consume the final beamformed stream as a meeting microphone through `Loopback Audio`.

## Mac Receiver

```bash
cd /Users/omarghabyen/Desktop/FocusField
python3 -m focusfield.tools.rtp_loopback_rx \
  --bind 0.0.0.0 \
  --port 5004 \
  --device "Loopback Audio"
```

After the receiver starts, select **Loopback Audio** as the microphone in Zoom, Meet, or Teams.

## Pi Sender

Set the Mac IP on the Pi and run the RTP config:

```bash
export FOCUSFIELD_RTP_HOST="<MAC_IP>"
cd /home/focus/FocusField
python3 -m focusfield.main.run --config configs/meeting_peripheral_rtp.yaml
```

## Notes

- Transport is `RTP/L16`, mono, `48 kHz`, `480` samples per packet. This keeps UDP datagrams below a normal 1500-byte MTU and avoids IP fragmentation.
- The receiver smooths small packet gaps instead of stalling playout or inserting hard zero edges.
- The sender uses `audio.enhanced.final`, not raw capture or beamformed pre-denoise audio.

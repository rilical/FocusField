"""
CONTRACT: inline (source: src/focusfield/ui/views/live.md)
ROLE: Live dashboard view rendering.

INPUTS:
  - Topic: ui.telemetry  Type: TelemetrySnapshot
OUTPUTS:
  - Topic: n/a  Type: n/a

CONFIG KEYS:
  - ui.views.live.enabled: enable live view

PERF / TIMING:
  - render per telemetry update

FAILURE MODES:
  - render error -> log render_failed

LOG EVENTS:
  - module=ui.views.live, event=render_failed, payload keys=error

TESTS:
  - n/a

CONTRACT DETAILS (inline from src/focusfield/ui/views/live.md):
# Live view

- Camera tiles (1 or 3) with face overlays.
- Polar heatmap visualization.
- Lock state and event log.
"""

from __future__ import annotations


def live_page() -> str:
    """Return a minimal live UI page."""
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>FocusField Live</title>
    <style>
      @import url("https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600&family=IBM+Plex+Sans:wght@300;500&display=swap");
      :root {
        --bg: #f4efe8;
        --ink: #1f1b16;
        --accent: #e35d2f;
        --accent-2: #2f7e7e;
        --panel: #ffffff;
        --shadow: rgba(0, 0, 0, 0.08);
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "IBM Plex Sans", sans-serif;
        background: radial-gradient(circle at 20% 10%, #ffe5d6, var(--bg) 45%);
        color: var(--ink);
      }
      header {
        padding: 18px 24px;
        display: flex;
        align-items: baseline;
        gap: 12px;
      }
      header h1 {
        font-family: "Space Grotesk", sans-serif;
        font-size: 22px;
        margin: 0;
        letter-spacing: 0.5px;
      }
      header span {
        font-size: 14px;
        opacity: 0.6;
      }
      main {
        display: grid;
        grid-template-columns: minmax(280px, 2fr) minmax(240px, 1fr);
        gap: 18px;
        padding: 0 24px 24px;
      }
      .panel {
        background: var(--panel);
        border-radius: 16px;
        box-shadow: 0 10px 30px var(--shadow);
        padding: 16px;
      }
      .camera-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 16px;
      }
      .camera-tile {
        position: relative;
        border-radius: 12px;
        overflow: hidden;
        background: #111;
      }
      .camera-tile canvas {
        width: 100%;
        display: block;
      }
      .camera-label {
        position: absolute;
        bottom: 8px;
        left: 8px;
        background: rgba(0, 0, 0, 0.6);
        color: #fff;
        padding: 4px 8px;
        border-radius: 8px;
        font-size: 12px;
      }
      .side-stack {
        display: grid;
        gap: 16px;
      }
      .heatmap {
        display: flex;
        align-items: center;
        justify-content: center;
      }
      .heatmap canvas {
        width: 100%;
        max-width: 260px;
        aspect-ratio: 1 / 1;
      }
      .lock-status h2 {
        margin: 0 0 8px;
        font-family: "Space Grotesk", sans-serif;
        font-size: 18px;
      }
      .lock-status p {
        margin: 4px 0;
        font-size: 14px;
      }
      .pill {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 999px;
        background: var(--accent-2);
        color: #fff;
        font-size: 12px;
      }
      @media (max-width: 900px) {
        main {
          grid-template-columns: 1fr;
        }
      }
    </style>
  </head>
  <body>
    <header>
      <h1>FocusField Live</h1>
      <span>Vision-first demo</span>
    </header>
    <main>
      <section class="panel">
        <div class="camera-grid" id="cameraGrid"></div>
      </section>
      <aside class="side-stack">
        <section class="panel heatmap">
          <canvas id="heatmapCanvas" width="280" height="280"></canvas>
        </section>
        <section class="panel lock-status">
          <h2>Target Lock</h2>
          <p><span class="pill" id="lockState">NO_LOCK</span></p>
          <p id="lockInfo">Waiting for speaking face...</p>
        </section>
      </aside>
    </main>
    <script>
      const cameras = ["cam0", "cam1", "cam2"];
      const cameraGrid = document.getElementById("cameraGrid");
      const tiles = {};

      function ensureTile(cameraId) {
        if (tiles[cameraId]) return tiles[cameraId];
        const tile = document.createElement("div");
        tile.className = "camera-tile";
        const canvas = document.createElement("canvas");
        canvas.dataset.cameraId = cameraId;
        const label = document.createElement("div");
        label.className = "camera-label";
        label.textContent = cameraId;
        tile.appendChild(canvas);
        tile.appendChild(label);
        cameraGrid.appendChild(tile);
        tiles[cameraId] = { canvas, ctx: canvas.getContext("2d"), label };
        return tiles[cameraId];
      }

      function drawFrame(cameraId, faces) {
        const tile = ensureTile(cameraId);
        const img = new Image();
        img.onload = () => {
          tile.canvas.width = img.naturalWidth;
          tile.canvas.height = img.naturalHeight;
          tile.ctx.drawImage(img, 0, 0);
          for (const face of faces) {
            const bbox = face.bbox;
            if (!bbox) continue;
            const color = face.speaking ? "#e35d2f" : "rgba(255,255,255,0.7)";
            tile.ctx.strokeStyle = color;
            tile.ctx.lineWidth = 2;
            tile.ctx.strokeRect(bbox.x, bbox.y, bbox.w, bbox.h);
            tile.ctx.fillStyle = color;
            tile.ctx.font = "12px IBM Plex Sans";
            tile.ctx.fillText(
              `${face.track_id} ${face.mouth_activity?.toFixed(2) ?? ""}`,
              bbox.x,
              Math.max(12, bbox.y - 4)
            );
          }
        };
        img.src = `/frame/${cameraId}.jpg?ts=${Date.now()}`;
      }

      function drawHeatmap(heatmap) {
        const canvas = document.getElementById("heatmapCanvas");
        const ctx = canvas.getContext("2d");
        const cx = canvas.width / 2;
        const cy = canvas.height / 2;
        const radius = Math.min(cx, cy) * 0.85;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.strokeStyle = "rgba(0,0,0,0.1)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(cx, cy, radius, 0, Math.PI * 2);
        ctx.stroke();
        if (!heatmap || !heatmap.heatmap) return;
        const bins = heatmap.heatmap.length;
        for (let i = 0; i < bins; i++) {
          const value = heatmap.heatmap[i] || 0;
          const angle = (i / bins) * Math.PI * 2 - Math.PI / 2;
          const bar = radius * value;
          const x1 = cx + Math.cos(angle) * (radius - bar);
          const y1 = cy + Math.sin(angle) * (radius - bar);
          const x2 = cx + Math.cos(angle) * radius;
          const y2 = cy + Math.sin(angle) * radius;
          ctx.strokeStyle = `rgba(227,93,47,${0.2 + value * 0.8})`;
          ctx.beginPath();
          ctx.moveTo(x1, y1);
          ctx.lineTo(x2, y2);
          ctx.stroke();
        }
      }

      async function update() {
        const response = await fetch(`/telemetry?ts=${Date.now()}`);
        if (!response.ok) return;
        const data = await response.json();
        const facesByCamera = {};
        for (const face of data.face_summaries || []) {
          const cameraId = face.camera_id || "cam0";
          facesByCamera[cameraId] = facesByCamera[cameraId] || [];
          facesByCamera[cameraId].push(face);
        }
        for (const cameraId of cameras) {
          drawFrame(cameraId, facesByCamera[cameraId] || []);
        }
        drawHeatmap(data.heatmap_summary);
        const lock = data.lock_state || {};
        document.getElementById("lockState").textContent = lock.state || "NO_LOCK";
        document.getElementById("lockInfo").textContent =
          lock.state === "NO_LOCK"
            ? "Waiting for speaking face..."
            : `Target: ${lock.target_id || "n/a"} | ${lock.target_bearing_deg?.toFixed(1) ?? "?"}° | ${lock.reason || ""}`;
      }

      setInterval(update, 120);
    </script>
  </body>
</html>
"""

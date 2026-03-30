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
    """Return the operator dashboard UI page."""
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>FocusField Live</title>
  <style>
    :root {
      --bg:        #0d1117;
      --panel:     #161b22;
      --border:    #30363d;
      --text:      #e6edf3;
      --text-sec:  #8b949e;
      --green:     #3fb950;
      --yellow:    #d29922;
      --red:       #f85149;
      --blue:      #58a6ff;
      --orange:    #f0883e;
      --panel-r:   6px;
      --gap:       10px;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { height: 100%; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
      font-size: 13px;
      display: flex;
      flex-direction: column;
      min-height: 100vh;
      overflow-x: hidden;
    }

    /* ── Header ── */
    #header {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 14px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      flex-shrink: 0;
    }
    #header .logo {
      font-size: 15px;
      font-weight: 700;
      letter-spacing: 0.04em;
      color: var(--text);
    }
    #header .logo span { color: var(--green); }
    #ws-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--red);
      flex-shrink: 0;
      transition: background 0.3s;
    }
    #ws-label { color: var(--text-sec); font-size: 11px; }
    #header-right { margin-left: auto; display: flex; align-items: center; gap: 14px; }
    #runtime-badges {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--text-sec);
      font-size: 10px;
      max-width: 520px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    #focus-badges {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--text-sec);
      font-size: 10px;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }
    #perf-badge {
      font-size: 11px; color: var(--text-sec);
      font-variant-numeric: tabular-nums;
    }
    #health-pill {
      font-size: 11px; padding: 2px 8px; border-radius: 3px;
      background: rgba(63,185,80,0.15); color: var(--green);
      border: 1px solid rgba(63,185,80,0.3);
    }
    #calib-toggle {
      font-size: 11px; padding: 3px 9px; border-radius: 3px;
      background: transparent; color: var(--text-sec);
      border: 1px solid var(--border); cursor: pointer;
    }
    #calib-toggle:hover { color: var(--text); border-color: var(--text-sec); }

    /* ── Main grid ── */
    #main {
      flex: 1;
      display: grid;
      grid-template-rows: auto auto auto auto;
      grid-template-columns: 1fr 1fr 1fr;
      gap: var(--gap);
      padding: var(--gap);
    }

    /* Row 1: 3 cameras */
    .cam-tile {
      grid-row: 1;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--panel-r);
      overflow: hidden;
      position: relative;
      transition: border-color 0.25s;
    }
    .cam-tile.active-cam { border-color: var(--green); box-shadow: 0 0 0 1px var(--green); }

    .cam-tile-inner {
      position: relative;
      width: 100%;
      aspect-ratio: 4/3;
      background: #000;
      overflow: hidden;
    }
    .cam-tile-inner img {
      position: absolute; inset: 0; width: 100%; height: 100%;
      object-fit: cover; display: block;
    }
    .cam-tile-inner canvas {
      position: absolute; inset: 0; width: 100%; height: 100%;
      pointer-events: none;
    }
    .cam-label-bar {
      position: absolute; top: 0; left: 0; right: 0;
      display: flex; align-items: center; gap: 6px;
      padding: 5px 8px;
      background: linear-gradient(to bottom, rgba(13,17,23,0.85) 0%, transparent 100%);
    }
    .cam-conn-dot {
      width: 6px; height: 6px; border-radius: 50%; background: var(--red); flex-shrink: 0;
    }
    .cam-name { font-size: 11px; font-weight: 600; color: var(--text); }
    .cam-lock-badge {
      margin-left: auto;
      font-size: 10px; font-weight: 700; letter-spacing: 0.06em;
      padding: 2px 6px; border-radius: 3px;
      background: rgba(22,27,34,0.85);
      color: var(--text-sec);
      border: 1px solid var(--border);
    }
    .cam-lock-badge.NO_LOCK  { color: var(--text-sec); border-color: var(--border); }
    .cam-lock-badge.ACQUIRE  { color: var(--yellow);   border-color: rgba(210,153,34,0.4); animation: pulse-yellow 1s infinite; }
    .cam-lock-badge.LOCKED   { color: var(--green);    border-color: rgba(63,185,80,0.4); }
    .cam-lock-badge.HOLD     { color: var(--blue);     border-color: rgba(88,166,255,0.4); }
    @keyframes pulse-yellow {
      0%,100% { opacity: 1; } 50% { opacity: 0.55; }
    }

    /* Row 2: Compass | MicHealth | AudioMeters */
    #row2 {
      grid-row: 2;
      grid-column: 1 / 4;
      display: grid;
      grid-template-columns: 300px 1fr 1fr;
      gap: var(--gap);
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--panel-r);
      padding: 10px 12px;
      overflow: hidden;
    }
    .panel-title {
      font-size: 10px; font-weight: 700; letter-spacing: 0.1em;
      text-transform: uppercase; color: var(--text-sec);
      margin-bottom: 8px; display: flex; align-items: center; gap: 6px;
    }
    .panel-title .badge {
      font-size: 10px; padding: 1px 5px; border-radius: 2px;
      background: rgba(88,166,255,0.12); color: var(--blue);
      font-weight: 600; letter-spacing: 0.04em;
    }

    /* Compass */
    #compass-wrap {
      display: flex; flex-direction: column; align-items: center;
    }
    #heatmapCanvas { display: block; }

    /* Mic health bars */
    #mic-bars-wrap {
      display: flex; align-items: flex-end; gap: 5px; height: 100px;
    }
    .mic-ch-col {
      flex: 1; display: flex; flex-direction: column; align-items: center; gap: 3px;
    }
    .mic-ch-bar-wrap {
      flex: 1; width: 100%; position: relative;
      background: rgba(255,255,255,0.04); border-radius: 3px; overflow: hidden;
    }
    .mic-ch-bar-fill {
      position: absolute; bottom: 0; left: 0; right: 0;
      border-radius: 3px 3px 0 0; transition: height 0.15s;
    }
    .mic-ch-trust-fill {
      position: absolute; bottom: 0; left: 0; right: 0;
      border-radius: 3px 3px 0 0; opacity: 0.22;
      background: var(--blue);
    }
    .mic-ch-label { font-size: 9px; color: var(--text-sec); }
    .mic-ch-reason { font-size: 8px; color: var(--red); text-align: center; max-width: 100%; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
    #mic-summary { font-size: 11px; color: var(--text-sec); margin-bottom: 6px; display: flex; gap: 14px; }
    #mic-summary span { color: var(--text); font-variant-numeric: tabular-nums; }

    /* Audio meters */
    #audio-wrap { display: flex; flex-direction: column; gap: 4px; }
    .rms-row {
      display: flex; align-items: center; gap: 6px;
    }
    .rms-ch-label { font-size: 9px; color: var(--text-sec); width: 18px; text-align: right; flex-shrink: 0; }
    .rms-bar-track {
      flex: 1; height: 8px; background: rgba(255,255,255,0.04);
      border-radius: 2px; overflow: hidden; position: relative;
    }
    .rms-bar-fill {
      position: absolute; left: 0; top: 0; bottom: 0;
      border-radius: 2px; transition: width 0.1s;
      background: var(--green);
    }
    .rms-snr { font-size: 9px; color: var(--text-sec); width: 36px; text-align: right; flex-shrink: 0; font-variant-numeric: tabular-nums; }
    #vad-row {
      display: flex; align-items: center; gap: 8px; margin-top: 4px; padding-top: 6px;
      border-top: 1px solid var(--border);
    }
    #vad-dot {
      width: 14px; height: 14px; border-radius: 50%;
      background: var(--text-sec); flex-shrink: 0; transition: background 0.2s;
    }
    #vad-label { font-size: 11px; color: var(--text-sec); }
    #vad-conf-track {
      flex: 1; height: 6px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden;
    }
    #vad-conf-fill {
      height: 100%; border-radius: 3px; background: var(--green); transition: width 0.15s;
    }

    /* Row 3: Timeline */
    #row3 {
      grid-row: 3; grid-column: 1 / 4;
    }
    #timeline-canvas { display: block; width: 100%; }

    /* Row 4: Log */
    #row4 {
      grid-row: 4; grid-column: 1 / 4;
    }
    #log-body {
      font-family: "SF Mono", "Cascadia Code", "Fira Code", monospace;
      font-size: 11px; line-height: 1.5;
      max-height: 140px; overflow-y: auto;
      color: var(--text-sec);
    }
    #log-body::-webkit-scrollbar { width: 4px; }
    #log-body::-webkit-scrollbar-track { background: transparent; }
    #log-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
    .log-line { padding: 1px 0; white-space: pre; overflow: hidden; text-overflow: ellipsis; }
    .log-line.error { color: var(--red); }
    .log-line.warning { color: var(--yellow); }
    .log-line.info { color: var(--text-sec); }
    .log-line.debug { color: #4a5568; }

    /* Calibration panel */
    #calib-panel {
      display: none;
      grid-row: 5; grid-column: 1 / 4;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--panel-r);
      padding: 10px 14px;
    }
    #calib-panel.open { display: block; }
    #calib-sliders { display: flex; gap: 20px; flex-wrap: wrap; margin-top: 8px; }
    .calib-cam-col { display: flex; flex-direction: column; gap: 4px; flex: 1; min-width: 160px; }
    .calib-cam-col label { font-size: 11px; color: var(--text-sec); }
    .calib-cam-col input[type=range] { width: 100%; accent-color: var(--blue); }
    .calib-cam-col .yaw-val { font-size: 11px; color: var(--text); font-variant-numeric: tabular-nums; }
    #calib-apply {
      margin-top: 10px; padding: 5px 14px; border-radius: 4px;
      background: var(--blue); color: #0d1117; border: none;
      font-weight: 700; font-size: 12px; cursor: pointer;
    }
    #calib-apply:hover { opacity: 0.85; }
  </style>
</head>
<body>

<!-- ── Header ── -->
<div id="header">
  <div class="logo">Focus<span>Field</span></div>
  <div id="ws-dot"></div>
  <div id="ws-label">connecting…</div>
  <div id="header-right">
    <div id="focus-badges"></div>
    <div id="runtime-badges"></div>
    <div id="perf-badge">queue age: —</div>
    <div id="health-pill">ok</div>
    <button id="calib-toggle">Calibration</button>
  </div>
</div>

<!-- ── Main grid ── -->
<div id="main">

  <!-- Row 1: cameras (built by JS) -->
  <div id="cam0-tile" class="cam-tile" style="grid-row:1;grid-column:1;">
    <div class="cam-tile-inner">
      <img id="cam0-img" src="" alt="" />
      <canvas id="cam0-canvas"></canvas>
      <div class="cam-label-bar">
        <div class="cam-conn-dot" id="cam0-conn"></div>
        <div class="cam-name" id="cam0-name">cam0</div>
        <div class="cam-lock-badge NO_LOCK" id="cam0-badge">NO_LOCK</div>
      </div>
    </div>
  </div>
  <div id="cam1-tile" class="cam-tile" style="grid-row:1;grid-column:2;">
    <div class="cam-tile-inner">
      <img id="cam1-img" src="" alt="" />
      <canvas id="cam1-canvas"></canvas>
      <div class="cam-label-bar">
        <div class="cam-conn-dot" id="cam1-conn"></div>
        <div class="cam-name" id="cam1-name">cam1</div>
        <div class="cam-lock-badge NO_LOCK" id="cam1-badge">NO_LOCK</div>
      </div>
    </div>
  </div>
  <div id="cam2-tile" class="cam-tile" style="grid-row:1;grid-column:3;">
    <div class="cam-tile-inner">
      <img id="cam2-img" src="" alt="" />
      <canvas id="cam2-canvas"></canvas>
      <div class="cam-label-bar">
        <div class="cam-conn-dot" id="cam2-conn"></div>
        <div class="cam-name" id="cam2-name">cam2</div>
        <div class="cam-lock-badge NO_LOCK" id="cam2-badge">NO_LOCK</div>
      </div>
    </div>
  </div>

  <!-- Row 2 -->
  <div id="row2">
    <!-- Compass -->
    <div class="panel" id="compass-wrap">
      <div class="panel-title">DOA Compass</div>
      <canvas id="heatmapCanvas" width="276" height="276"></canvas>
    </div>

    <!-- Mic health -->
    <div class="panel" id="mic-panel">
      <div class="panel-title">
        Mic Health
        <div class="badge" id="mic-badge-mean">—</div>
      </div>
      <div id="mic-summary">
        <div>score <span id="mic-mean-score">—</span></div>
        <div>trust <span id="mic-mean-trust">—</span></div>
      </div>
      <div id="mic-bars-wrap"></div>
    </div>

    <!-- Audio meters -->
    <div class="panel" id="audio-panel">
      <div class="panel-title">Audio Meters</div>
      <div id="audio-wrap"></div>
      <div id="vad-row">
        <div id="vad-dot"></div>
        <div id="vad-label">VAD: —</div>
        <div id="vad-conf-track"><div id="vad-conf-fill" style="width:0%"></div></div>
      </div>
    </div>
  </div>

  <!-- Row 3: Timeline -->
  <div class="panel" id="row3">
    <div class="panel-title">Lock State Timeline <span style="font-weight:400;color:var(--text-sec);margin-left:4px;">— last 60 s</span></div>
    <canvas id="timeline-canvas" height="48"></canvas>
  </div>

  <!-- Row 4: Log -->
  <div class="panel" id="row4">
    <div class="panel-title">Event Log</div>
    <div id="log-body"></div>
  </div>

  <!-- Calibration (row 5, hidden by default) -->
  <div id="calib-panel">
    <div class="panel-title">Camera Yaw Calibration</div>
    <div id="calib-sliders"></div>
    <button id="calib-apply">Apply</button>
    <span id="calib-status" style="font-size:11px;color:var(--text-sec);margin-left:10px;"></span>
  </div>

</div><!-- #main -->

<script>
(function() {
'use strict';

/* ── Constants ── */
const CAM_IDS        = ['cam0', 'cam1', 'cam2'];
const FACE_HOLD_MS   = 700;
const DB_MIN         = -60;
const DB_MAX         = 0;
const TIMELINE_SECS  = 60;
const STATE_COLORS   = {
  'NO_LOCK': '#8b949e',
  'ACQUIRE': '#d29922',
  'LOCKED':  '#3fb950',
  'HOLD':    '#58a6ff',
};

/* ── State ── */
let cameraMap        = [];           // [{id,yaw_offset_deg,hfov_deg}]
let cameraYaw        = {};           // id -> deg
let cameraHfov       = {};           // id -> deg
let lastFaces        = {};           // camId -> faces[]
let lastFacesTs      = {};           // camId -> ms
let lastImgLoadTs    = {};           // camId -> ms
let imgConnected     = {};           // camId -> bool
let frameScheduled   = {};          // camId -> bool

// timeline ring buffer: {t_ms, state, bearing}
const TL_MAX         = 600;         // 10 pts/s × 60s
let tlRing           = [];
let lastTelemetry    = null;

/* ── DOM refs ── */
const wsDot          = document.getElementById('ws-dot');
const wsLabel        = document.getElementById('ws-label');
const healthPill     = document.getElementById('health-pill');
const perfBadge      = document.getElementById('perf-badge');
const focusBadges    = document.getElementById('focus-badges');
const runtimeBadges  = document.getElementById('runtime-badges');
const heatmapCanvas  = document.getElementById('heatmapCanvas');
const heatCtx        = heatmapCanvas.getContext('2d');
const tlCanvas       = document.getElementById('timeline-canvas');
const tlCtx          = tlCanvas.getContext('2d');
const logBody        = document.getElementById('log-body');
const calibPanel     = document.getElementById('calib-panel');
const calibSliders   = document.getElementById('calib-sliders');
const calibApply     = document.getElementById('calib-apply');
const calibStatus    = document.getElementById('calib-status');
const calibToggle    = document.getElementById('calib-toggle');

/* ── Calibration toggle ── */
calibToggle.addEventListener('click', () => {
  calibPanel.classList.toggle('open');
});

/* ── WebSocket ── */
let ws;
let httpFallback = null;

function connectWS() {
  try {
    ws = new WebSocket('ws://' + location.host + '/ws');
  } catch(e) {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    wsDot.style.background  = '#3fb950';
    wsLabel.textContent      = 'live';
    stopHTTPFallback();
  };
  ws.onclose = () => {
    wsDot.style.background  = '#f85149';
    wsLabel.textContent      = 'disconnected — polling';
    setTimeout(connectWS, 2000);
    fallbackToHTTP();
  };
  ws.onerror = () => { ws.close(); };
  ws.onmessage = (e) => {
    try { updateAllPanels(JSON.parse(e.data)); } catch(_) {}
  };
}

function scheduleReconnect() {
  setTimeout(connectWS, 2000);
  fallbackToHTTP();
}

function fallbackToHTTP() {
  if (httpFallback) return;
  httpFallback = setInterval(async () => {
    try {
      const r = await fetch('/telemetry');
      if (r.ok) updateAllPanels(await r.json());
    } catch(_) {}
  }, 500);
}

function stopHTTPFallback() {
  if (httpFallback) { clearInterval(httpFallback); httpFallback = null; }
}

/* ═══════════════════════════════════════════════
   UPDATE ALL PANELS
   ═══════════════════════════════════════════════ */
function updateAllPanels(data) {
  lastTelemetry = data;

  // Pull camera meta
  const meta = data.meta || {};
  if (Array.isArray(meta.camera_map)) {
    cameraMap = meta.camera_map;
    for (const c of cameraMap) {
      cameraYaw[c.id]  = Number(c.yaw_offset_deg ?? 0);
      cameraHfov[c.id] = Number(c.hfov_deg ?? 160);
    }
  }

  const lock   = data.lock_state   || {};
  const state  = lock.state        || 'NO_LOCK';
  const target = lock.target_bearing_deg;
  const facesByCamera = groupFacesByCamera(data.face_summaries || []);

  // Camera panels
  for (const camId of CAM_IDS) {
    updateCameraPanel(camId, facesByCamera[camId] || [], lock);
  }

  // Compass
  drawHeatmap(data.heatmap_summary, lock, data.beamformer);

  // Mic health
  updateMicHealth(data.mic_health);

  // Audio meters
  updateAudioMeters(data.mic_health, data.fusion_debug);

  // Timeline
  pushTimeline(state, target);
  drawTimeline();

  // Log
  updateLog(data.logs);

  // Header badges
  updateHeader(data);

  // Calibration
  updateCalibration(data.meta);
}

/* ─── Helpers ─── */
function groupFacesByCamera(faces) {
  const m = {};
  for (const f of faces) {
    const c = f.camera_id || 'cam0';
    if (!m[c]) m[c] = [];
    m[c].push(f);
  }
  return m;
}

/* ═══════════════════════════════════════════════
   CAMERA PANEL
   ═══════════════════════════════════════════════ */
function camInFov(camId, bearingDeg) {
  if (bearingDeg == null || !Number.isFinite(bearingDeg)) return false;
  const yaw  = cameraYaw[camId]  ?? null;
  const hfov = cameraHfov[camId] ?? 160;
  if (yaw == null) return false;
  let diff = ((bearingDeg - yaw) % 360 + 360) % 360;
  if (diff > 180) diff -= 360;
  return Math.abs(diff) <= hfov / 2;
}

function updateCameraPanel(camId, freshFaces, lock) {
  const state    = lock.state || 'NO_LOCK';
  const targetId = lock.target_id || null;
  const bearingDeg = lock.target_bearing_deg;
  const nowMs    = Date.now();

  // Determine faces with hold
  if (freshFaces.length > 0) {
    lastFaces[camId]   = freshFaces;
    lastFacesTs[camId] = nowMs;
  }
  const cachedFaces = lastFaces[camId] || [];
  const ageMs       = nowMs - (lastFacesTs[camId] || 0);
  const faces       = freshFaces.length > 0
    ? freshFaces
    : (ageMs <= FACE_HOLD_MS ? cachedFaces : []);

  // Tile highlight
  const tile  = document.getElementById(camId + '-tile');
  const inFov = camInFov(camId, bearingDeg);
  if (tile) tile.classList.toggle('active-cam', inFov);

  // Name badge
  const nameEl = document.getElementById(camId + '-name');
  if (nameEl) {
    const yaw = cameraYaw[camId];
    nameEl.textContent = Number.isFinite(yaw) ? camId + ' @ ' + Math.round(yaw) + '\u00b0' : camId;
  }

  // Lock badge
  const badge = document.getElementById(camId + '-badge');
  if (badge) {
    badge.textContent = state;
    badge.className   = 'cam-lock-badge ' + state;
  }

  // Schedule frame draw
  if (!frameScheduled[camId]) {
    frameScheduled[camId] = true;
    requestAnimationFrame(() => {
      frameScheduled[camId] = false;
      loadAndDrawFrame(camId, faces, targetId);
    });
  }
}

function loadAndDrawFrame(camId, faces, targetId) {
  const imgEl    = document.getElementById(camId + '-img');
  const canvasEl = document.getElementById(camId + '-canvas');
  const connDot  = document.getElementById(camId + '-conn');
  if (!imgEl || !canvasEl) return;

  const now = Date.now();
  // Throttle to ~6fps (166ms)
  if (now - (lastImgLoadTs[camId] || 0) < 150) {
    // still redraw overlays on existing image
    drawOverlay(canvasEl, imgEl, faces, targetId);
    return;
  }
  lastImgLoadTs[camId] = now;

  const src = '/frame/' + camId + '.jpg?ts=' + now;
  const img = new Image();
  img.onload = () => {
    imgConnected[camId] = true;
    if (connDot) connDot.style.background = '#3fb950';
    imgEl.src = img.src;
    drawOverlay(canvasEl, imgEl, faces, targetId);
  };
  img.onerror = () => {
    imgConnected[camId] = false;
    if (connDot) connDot.style.background = '#f85149';
  };
  img.src = src;
}

function drawOverlay(canvas, img, faces, targetId) {
  // Size canvas to match image natural size
  const nw = img.naturalWidth  || 320;
  const nh = img.naturalHeight || 240;
  if (canvas.width !== nw)  canvas.width  = nw;
  if (canvas.height !== nh) canvas.height = nh;

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, nw, nh);

  for (const face of faces) {
    const bbox = face.bbox;
    if (!bbox) continue;
    const isTarget   = targetId && face.track_id === targetId;
    const isSpeaking = face.speaking;

    let color;
    if (isTarget)        color = '#3fb950';
    else if (isSpeaking) color = '#f0883e';
    else                 color = 'rgba(255,255,255,0.65)';

    ctx.strokeStyle = color;
    ctx.lineWidth   = isTarget ? 2.5 : 1.5;
    ctx.strokeRect(bbox.x, bbox.y, bbox.w, bbox.h);

    // Label
    const mouthStr = face.mouth_activity != null ? face.mouth_activity.toFixed(2) : '';
    const statusStr = isSpeaking ? 'TALK' : 'idle';
    const label = face.track_id + ' ' + statusStr + ' ' + mouthStr;
    ctx.font      = '11px system-ui, sans-serif';
    ctx.fillStyle = color;
    const ty = Math.max(13, bbox.y - 3);
    ctx.fillText(label, bbox.x + 1, ty);
  }
}

/* ═══════════════════════════════════════════════
   HEATMAP / COMPASS
   ═══════════════════════════════════════════════ */
function degToRad(deg) { return deg * Math.PI / 180; }

// In our compass: 0deg=top (north), clockwise
// canvas angle: angle_from_east = deg - 90  (since east=0 in canvas)
function bearingToCanvas(bearingDeg) {
  return degToRad(bearingDeg - 90);
}

function drawHeatmap(heatmapSummary, lock, beamformer) {
  const W   = heatmapCanvas.width;
  const H   = heatmapCanvas.height;
  const cx  = W / 2;
  const cy  = H / 2;
  const R   = Math.min(cx, cy) * 0.82;

  heatCtx.clearRect(0, 0, W, H);

  // Background circle
  heatCtx.beginPath();
  heatCtx.arc(cx, cy, R, 0, Math.PI * 2);
  heatCtx.fillStyle = 'rgba(255,255,255,0.02)';
  heatCtx.fill();

  // Grid rings at 33%, 66%, 100%
  heatCtx.strokeStyle = 'rgba(255,255,255,0.07)';
  heatCtx.lineWidth   = 0.5;
  for (const f of [0.33, 0.66, 1.0]) {
    heatCtx.beginPath();
    heatCtx.arc(cx, cy, R * f, 0, Math.PI * 2);
    heatCtx.stroke();
  }
  // Crosshairs
  heatCtx.beginPath();
  heatCtx.moveTo(cx, cy - R); heatCtx.lineTo(cx, cy + R);
  heatCtx.moveTo(cx - R, cy); heatCtx.lineTo(cx + R, cy);
  heatCtx.stroke();

  // Cardinal labels
  const cardinals = [['N', 0], ['E', 90], ['S', 180], ['W', 270]];
  heatCtx.font      = '10px system-ui, sans-serif';
  heatCtx.fillStyle = 'rgba(139,148,158,0.8)';
  heatCtx.textAlign = 'center';
  heatCtx.textBaseline = 'middle';
  for (const [lbl, deg] of cardinals) {
    const a  = bearingToCanvas(deg);
    const lx = cx + Math.cos(a) * (R + 12);
    const ly = cy + Math.sin(a) * (R + 12);
    heatCtx.fillText(lbl, lx, ly);
  }

  // DOA energy bars
  if (heatmapSummary && Array.isArray(heatmapSummary.heatmap)) {
    const bins = heatmapSummary.heatmap;
    const n    = bins.length;
    const maxV = Math.max(...bins, 0.001);
    for (let i = 0; i < n; i++) {
      const v     = (bins[i] || 0) / maxV;
      const bearingDeg = (i / n) * 360;
      const a     = bearingToCanvas(bearingDeg);
      const rInner = R * 0.08;
      const rOuter = rInner + (R - rInner) * v;
      heatCtx.strokeStyle = 'rgba(240,136,62,' + (0.15 + v * 0.85) + ')';
      heatCtx.lineWidth   = Math.max(1.5, (360 / n) * (Math.PI * R / 180) * 0.7);
      heatCtx.beginPath();
      heatCtx.moveTo(cx + Math.cos(a) * rInner, cy + Math.sin(a) * rInner);
      heatCtx.lineTo(cx + Math.cos(a) * rOuter, cy + Math.sin(a) * rOuter);
      heatCtx.stroke();
    }
  }

  // Camera FOV wedges and perimeter dots
  for (const cam of cameraMap) {
    const yaw   = Number(cam.yaw_offset_deg ?? 0);
    const hfov  = Number(cam.hfov_deg ?? 160);
    const aCenter = bearingToCanvas(yaw);
    const halfFov = degToRad(hfov / 2);

    // FOV wedge
    heatCtx.beginPath();
    heatCtx.moveTo(cx, cy);
    heatCtx.arc(cx, cy, R, aCenter - halfFov, aCenter + halfFov);
    heatCtx.closePath();
    heatCtx.fillStyle   = 'rgba(88,166,255,0.06)';
    heatCtx.fill();
    heatCtx.strokeStyle = 'rgba(88,166,255,0.2)';
    heatCtx.lineWidth   = 1;
    heatCtx.stroke();

    // Perimeter dot
    const dotR = R + 14;
    const dx   = cx + Math.cos(aCenter) * dotR;
    const dy   = cy + Math.sin(aCenter) * dotR;
    heatCtx.beginPath();
    heatCtx.arc(dx, dy, 5, 0, Math.PI * 2);
    heatCtx.fillStyle = '#58a6ff';
    heatCtx.fill();

    // Label
    heatCtx.font         = '9px system-ui, sans-serif';
    heatCtx.fillStyle    = '#8b949e';
    heatCtx.textAlign    = 'center';
    heatCtx.textBaseline = 'middle';
    const lblR = dotR + 12;
    heatCtx.fillText(cam.id, cx + Math.cos(aCenter) * lblR, cy + Math.sin(aCenter) * lblR);
  }

  // Beamformer steering direction
  if (beamformer && beamformer.target_bearing_deg != null) {
    const bfDeg = Number(beamformer.target_bearing_deg);
    if (Number.isFinite(bfDeg)) {
      const a  = bearingToCanvas(bfDeg);
      const bx = cx + Math.cos(a) * (R * 0.92);
      const by = cy + Math.sin(a) * (R * 0.92);
      heatCtx.strokeStyle = 'rgba(88,166,255,0.5)';
      heatCtx.lineWidth   = 1.5;
      heatCtx.setLineDash([4, 3]);
      heatCtx.beginPath();
      heatCtx.moveTo(cx, cy);
      heatCtx.lineTo(bx, by);
      heatCtx.stroke();
      heatCtx.setLineDash([]);
    }
  }

  // Target bearing arrow (green)
  const targetDeg = lock && lock.target_bearing_deg;
  if (targetDeg != null && Number.isFinite(Number(targetDeg))) {
    const a  = bearingToCanvas(Number(targetDeg));
    const tx = cx + Math.cos(a) * (R * 0.88);
    const ty = cy + Math.sin(a) * (R * 0.88);
    heatCtx.strokeStyle = '#3fb950';
    heatCtx.lineWidth   = 2;
    heatCtx.beginPath();
    heatCtx.moveTo(cx, cy);
    heatCtx.lineTo(tx, ty);
    heatCtx.stroke();

    // Arrowhead
    const headLen  = 8;
    const headAngle = 0.4;
    heatCtx.fillStyle = '#3fb950';
    heatCtx.beginPath();
    heatCtx.moveTo(tx, ty);
    heatCtx.lineTo(
      tx - headLen * Math.cos(a - headAngle),
      ty - headLen * Math.sin(a - headAngle)
    );
    heatCtx.lineTo(
      tx - headLen * Math.cos(a + headAngle),
      ty - headLen * Math.sin(a + headAngle)
    );
    heatCtx.closePath();
    heatCtx.fill();

    // Label
    const lblDist = R * 0.75;
    heatCtx.font         = '10px system-ui, sans-serif';
    heatCtx.fillStyle    = '#3fb950';
    heatCtx.textAlign    = 'center';
    heatCtx.textBaseline = 'middle';
    heatCtx.fillText(
      Number(targetDeg).toFixed(1) + '\u00b0',
      cx + Math.cos(a) * (lblDist + 10),
      cy + Math.sin(a) * (lblDist + 10)
    );
  }
}

/* ═══════════════════════════════════════════════
   MIC HEALTH PANEL
   ═══════════════════════════════════════════════ */
function scoreColor(s) {
  if (s >= 0.7)  return '#3fb950';
  if (s >= 0.35) return '#d29922';
  return '#f85149';
}

function updateMicHealth(micHealth) {
  const channels = (micHealth && micHealth.channels) || [];

  document.getElementById('mic-mean-score').textContent =
    micHealth && micHealth.mean_score != null ? micHealth.mean_score.toFixed(2) : '—';
  document.getElementById('mic-mean-trust').textContent =
    micHealth && micHealth.mean_trust != null ? micHealth.mean_trust.toFixed(2) : '—';

  const wrap = document.getElementById('mic-bars-wrap');
  // Build/reuse columns
  for (let i = 0; i < 8; i++) {
    let col = document.getElementById('mic-col-' + i);
    if (!col) {
      col           = document.createElement('div');
      col.className = 'mic-ch-col';
      col.id        = 'mic-col-' + i;
      col.innerHTML =
        '<div class="mic-ch-bar-wrap" id="mic-bw-' + i + '">' +
          '<div class="mic-ch-trust-fill" id="mic-trust-' + i + '"></div>' +
          '<div class="mic-ch-bar-fill" id="mic-fill-' + i + '"></div>' +
        '</div>' +
        '<div class="mic-ch-label">' + i + '</div>' +
        '<div class="mic-ch-reason" id="mic-reason-' + i + '"></div>';
      wrap.appendChild(col);
    }
    const ch    = channels[i] || {};
    const score = ch.score ?? 0;
    const trust = ch.trust ?? 0;
    const fill  = document.getElementById('mic-fill-' + i);
    const tfill = document.getElementById('mic-trust-' + i);
    const reason= document.getElementById('mic-reason-' + i);
    if (fill) { fill.style.height = (score * 100).toFixed(1) + '%'; fill.style.background = scoreColor(score); }
    if (tfill){ tfill.style.height = (trust * 100).toFixed(1) + '%'; }
    if (reason){ reason.textContent = ch.bad_reason || ''; }
  }
}

/* ═══════════════════════════════════════════════
   AUDIO METERS
   ═══════════════════════════════════════════════ */
function rmsToDb(rms) {
  if (!rms || rms <= 0) return DB_MIN;
  return Math.max(DB_MIN, 20 * Math.log10(rms));
}

function dbToFrac(db) {
  return (db - DB_MIN) / (DB_MAX - DB_MIN);
}

function updateAudioMeters(micHealth, fusionDebug) {
  const channels = (micHealth && micHealth.channels) || [];
  const wrap     = document.getElementById('audio-wrap');

  for (let i = 0; i < 8; i++) {
    let row = document.getElementById('rms-row-' + i);
    if (!row) {
      row           = document.createElement('div');
      row.className = 'rms-row';
      row.id        = 'rms-row-' + i;
      row.innerHTML =
        '<div class="rms-ch-label">' + i + '</div>' +
        '<div class="rms-bar-track"><div class="rms-bar-fill" id="rms-fill-' + i + '"></div></div>' +
        '<div class="rms-snr" id="rms-snr-' + i + '">—</div>';
      wrap.appendChild(row);
    }
    const ch  = channels[i] || {};
    const db  = rmsToDb(ch.rms);
    const frac = dbToFrac(db);
    const fill = document.getElementById('rms-fill-' + i);
    const snr  = document.getElementById('rms-snr-' + i);
    if (fill) {
      fill.style.width      = (frac * 100).toFixed(1) + '%';
      // Color: green > -20dB, yellow > -40dB, red otherwise
      fill.style.background = db > -20 ? '#3fb950' : db > -40 ? '#d29922' : '#f85149';
    }
    if (snr) {
      snr.textContent = ch.snr_db != null ? ch.snr_db.toFixed(1) + ' dB' : '—';
    }
  }

  // VAD
  const vadSpeech = fusionDebug && fusionDebug.vad_speech;
  const vadConf   = fusionDebug && fusionDebug.vad_confidence;
  const vadDot    = document.getElementById('vad-dot');
  const vadLabel  = document.getElementById('vad-label');
  const vadFill   = document.getElementById('vad-conf-fill');
  if (vadDot)  vadDot.style.background  = vadSpeech ? '#3fb950' : '#30363d';
  if (vadLabel) vadLabel.textContent    = 'VAD: ' + (vadSpeech ? 'SPEECH' : 'silence');
  if (vadFill && vadConf != null) vadFill.style.width = (vadConf * 100).toFixed(1) + '%';
}

/* ═══════════════════════════════════════════════
   TIMELINE
   ═══════════════════════════════════════════════ */
function pushTimeline(state, bearing) {
  tlRing.push({ t_ms: Date.now(), state, bearing });
  if (tlRing.length > TL_MAX) tlRing.shift();
}

function drawTimeline() {
  const W   = tlCanvas.parentElement.clientWidth - 24;  // panel padding
  if (W <= 0) return;
  tlCanvas.width  = W;
  const H   = tlCanvas.height;
  tlCtx.clearRect(0, 0, W, H);

  if (tlRing.length < 2) return;

  const now     = Date.now();
  const tStart  = now - TIMELINE_SECS * 1000;
  const tEnd    = now;
  const tRange  = tEnd - tStart;

  function xOf(t_ms) {
    return ((t_ms - tStart) / tRange) * W;
  }

  const bearingH = H * 0.45;  // top portion for bearing line
  const stateH   = H * 0.4;   // bottom strip height
  const stateY   = H - stateH;

  // Draw state segments
  for (let i = 0; i < tlRing.length - 1; i++) {
    const a  = tlRing[i];
    const b  = tlRing[i + 1];
    const x1 = Math.max(0, xOf(a.t_ms));
    const x2 = Math.min(W, xOf(b.t_ms));
    if (x2 < 0 || x1 > W) continue;
    tlCtx.fillStyle = STATE_COLORS[a.state] || '#8b949e';
    tlCtx.globalAlpha = 0.35;
    tlCtx.fillRect(x1, stateY, x2 - x1, stateH);
    tlCtx.globalAlpha = 1;
  }

  // Draw bearing line
  tlCtx.strokeStyle = '#3fb950';
  tlCtx.lineWidth   = 1.5;
  tlCtx.beginPath();
  let started = false;
  for (const pt of tlRing) {
    if (pt.bearing == null || !Number.isFinite(pt.bearing)) continue;
    const x  = xOf(pt.t_ms);
    const y  = bearingH * (1 - pt.bearing / 360);
    if (!started) { tlCtx.moveTo(x, y); started = true; }
    else          { tlCtx.lineTo(x, y); }
  }
  tlCtx.stroke();

  // Scale labels on left
  tlCtx.font      = '9px system-ui, sans-serif';
  tlCtx.fillStyle = '#4a5568';
  tlCtx.textAlign = 'left';
  tlCtx.textBaseline = 'top';
  tlCtx.fillText('360\u00b0', 2, 0);
  tlCtx.fillText('0\u00b0',   2, bearingH - 12);

  // Right-edge cursor line
  tlCtx.strokeStyle = 'rgba(255,255,255,0.15)';
  tlCtx.lineWidth   = 1;
  tlCtx.setLineDash([2, 3]);
  tlCtx.beginPath();
  tlCtx.moveTo(W - 1, 0);
  tlCtx.lineTo(W - 1, H);
  tlCtx.stroke();
  tlCtx.setLineDash([]);
}

/* ═══════════════════════════════════════════════
   LOG PANEL
   ═══════════════════════════════════════════════ */
const MAX_LOG_LINES = 20;
let renderedLogCount = 0;

function updateLog(logs) {
  if (!Array.isArray(logs) || logs.length === 0) return;
  // Only add new lines
  const newLines = logs.slice(-MAX_LOG_LINES);
  if (newLines.length === renderedLogCount && logs.length <= MAX_LOG_LINES) return;

  logBody.innerHTML = '';
  renderedLogCount  = 0;

  for (const entry of newLines.slice(-MAX_LOG_LINES)) {
    const div    = document.createElement('div');
    const level  = (entry.level || 'info').toLowerCase();
    div.className = 'log-line ' + level;
    const ts     = entry.t_ns != null
      ? new Date(Math.floor(entry.t_ns / 1e6)).toISOString().substr(11, 12)
      : '';
    const mod    = (entry.context && entry.context.module) ? entry.context.module : '';
    const msg    = entry.message || JSON.stringify(entry.context || {});
    div.textContent = ts + ' [' + level.toUpperCase().padEnd(5) + '] ' + (mod ? mod + ' ' : '') + msg;
    logBody.appendChild(div);
    renderedLogCount++;
  }
  logBody.scrollTop = logBody.scrollHeight;
}

/* ═══════════════════════════════════════════════
   HEADER
   ═══════════════════════════════════════════════ */
function updateHeader(data) {
  const healthSummary = (data && data.health_summary) || {};
  const perfSummary = (data && data.perf_summary) || {};
  const enhancedFinal = perfSummary && perfSummary.enhanced_final ? perfSummary.enhanced_final : {};
  const queueAge = enhancedFinal.pipeline_queue_age_ms != null
    ? enhancedFinal.pipeline_queue_age_ms
    : enhancedFinal.last_latency_ms;
  perfBadge.textContent = queueAge != null ? 'queue age: ' + Math.round(queueAge) + ' ms' : 'queue age: —';
  const lock = (data && data.lock_state) || {};
  const focusScore = lock.focus_score != null ? Number(lock.focus_score) : null;
  const activityScore = lock.activity_score != null ? Number(lock.activity_score) : null;
  const scoreMargin = lock.score_margin != null ? Number(lock.score_margin) : null;
  const runnerUpScore = lock.runner_up_focus_score != null ? Number(lock.runner_up_focus_score) : null;
  if (focusBadges) {
    const badges = [];
    badges.push('focus=' + (focusScore != null ? focusScore.toFixed(2) : '—'));
    badges.push('activity=' + (activityScore != null ? activityScore.toFixed(2) : '—'));
    badges.push('margin=' + (scoreMargin != null ? scoreMargin.toFixed(2) : '—'));
    if (runnerUpScore != null) badges.push('runner-up=' + runnerUpScore.toFixed(2));
    focusBadges.textContent = badges.join(' | ');
  }
  const badges = [];
  if (data && data.runtime_profile) badges.push('profile=' + data.runtime_profile);
  if (data && data.audio_fallback_active) badges.push('AUDIO_ONLY');
  if (data && data.detector_backend_active) badges.push('det=' + data.detector_backend_active);
  if (data && !data.strict_requirements_passed) badges.push('NON_STRICT');
  const visionDebug = (data && data.vision_debug) || {};
  if (visionDebug.detector_degraded) badges.push('DETECTOR_DEGRADED');
  const micSummary = (data && data.mic_health_summary) || {};
  if (Array.isArray(micSummary.dead_channels) && micSummary.dead_channels.length > 0) {
    badges.push('MIC_DEAD ' + micSummary.dead_channels.join(','));
  } else if (Array.isArray(micSummary.degraded_channels) && micSummary.degraded_channels.length > 0) {
    badges.push('MIC_DEG ' + micSummary.degraded_channels.join(','));
  }
  const dropCounts = (data && data.bus_drop_counts_window) || {};
  const totalDrops = Object.values(dropCounts).reduce((acc, v) => acc + Number(v || 0), 0);
  if (totalDrops > 0) badges.push('QUEUE_DROP ' + totalDrops);
  const captureOverflow = Number((data && data.capture_overflow_window) || 0);
  if (captureOverflow > 0) badges.push('CAPTURE_OVF ' + captureOverflow);
  if (runtimeBadges) runtimeBadges.textContent = badges.join(' | ');

  // Health
  const status = (healthSummary && healthSummary.status) || 'n/a';
  healthPill.textContent = status;
  if (status === 'ok') {
    healthPill.style.color      = '#3fb950';
    healthPill.style.background = 'rgba(63,185,80,0.1)';
    healthPill.style.borderColor = 'rgba(63,185,80,0.3)';
  } else {
    healthPill.style.color      = '#f85149';
    healthPill.style.background = 'rgba(248,81,73,0.1)';
    healthPill.style.borderColor = 'rgba(248,81,73,0.3)';
  }
}

/* ═══════════════════════════════════════════════
   CALIBRATION
   ═══════════════════════════════════════════════ */
let calibState = {};   // id -> yaw
let calibBuilt  = false;

function updateCalibration(meta) {
  if (!meta || !Array.isArray(meta.camera_map)) return;
  if (!calibBuilt) {
    calibSliders.innerHTML = '';
    for (const cam of meta.camera_map) {
      const id  = cam.id;
      const yaw = Number(cam.yaw_offset_deg ?? 0);
      calibState[id] = yaw;
      const col  = document.createElement('div');
      col.className = 'calib-cam-col';
      col.innerHTML =
        '<label>' + id + '</label>' +
        '<input type="range" min="0" max="360" step="1" value="' + yaw +
          '" id="calib-slider-' + id + '" />' +
        '<div class="yaw-val" id="calib-val-' + id + '">' + yaw + '\u00b0</div>';
      calibSliders.appendChild(col);

      const slider = document.getElementById('calib-slider-' + id);
      const valEl  = document.getElementById('calib-val-' + id);
      slider.addEventListener('input', () => {
        calibState[id]       = Number(slider.value);
        valEl.textContent    = slider.value + '\u00b0';
      });
    }
    calibBuilt = true;
  } else {
    // Update values from server without rebuilding
    for (const cam of meta.camera_map) {
      const slider = document.getElementById('calib-slider-' + cam.id);
      if (slider && document.activeElement !== slider) {
        slider.value = Number(cam.yaw_offset_deg ?? 0);
        const valEl = document.getElementById('calib-val-' + cam.id);
        if (valEl) valEl.textContent = Math.round(cam.yaw_offset_deg) + '\u00b0';
      }
    }
  }
}

calibApply.addEventListener('click', async () => {
  const payload = { cameras: [] };
  for (const [id, yaw] of Object.entries(calibState)) {
    payload.cameras.push({ id, yaw_offset_deg: yaw });
  }
  try {
    const r = await fetch('/api/camera-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    calibStatus.textContent = r.ok ? 'Saved.' : 'Error: ' + r.status;
  } catch(e) {
    calibStatus.textContent = 'Network error.';
  }
  setTimeout(() => { calibStatus.textContent = ''; }, 3000);
});

/* ═══════════════════════════════════════════════
   BOOT
   ═══════════════════════════════════════════════ */
connectWS();

// Periodic timeline redraw even without new data
setInterval(() => {
  if (lastTelemetry) drawTimeline();
}, 1000);

})();
</script>
</body>
</html>
"""

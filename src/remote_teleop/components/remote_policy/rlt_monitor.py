from __future__ import annotations

import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import cv2
import numpy as np


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Seal Box RLT Monitor</title>
  <style>
    :root { color-scheme: dark; --bg:#090d12; --panel:#111821; --line:#263140;
      --text:#e9f0f7; --muted:#8fa1b4; --vla:#39a8ff; --rlt:#ffae35;
      --human:#cf75ff; --ok:#42d392; --bad:#ff5d6c; --hold:#ffdc57; }
    * { box-sizing:border-box; }
    body { margin:0; background:radial-gradient(circle at 15% 0,#132133 0,var(--bg) 38%);
      color:var(--text); font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }
    main { max-width:1500px; margin:auto; padding:18px; }
    header { display:flex; align-items:center; justify-content:space-between; gap:14px; margin-bottom:14px; }
    h1 { font:700 20px/1.2 system-ui,sans-serif; margin:0; letter-spacing:.02em; }
    .live { display:flex; align-items:center; gap:8px; color:var(--muted); }
    .dot { width:9px; height:9px; border-radius:50%; background:var(--bad); box-shadow:0 0 12px currentColor; }
    .dot.ok { background:var(--ok); }
    .cameras { display:grid; grid-template-columns:2fr 1fr 1fr; gap:12px; }
    figure { margin:0; min-width:0; overflow:hidden; background:#06090d; border:1px solid var(--line); border-radius:12px; }
    figure img { display:block; width:100%; aspect-ratio:4/3; object-fit:cover; background:#05070a; }
    figcaption { padding:8px 11px; color:var(--muted); border-top:1px solid var(--line); }
    .status { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; margin-top:12px; }
    .card { background:color-mix(in srgb,var(--panel) 94%,transparent); border:1px solid var(--line);
      border-radius:10px; padding:11px; min-height:76px; }
    .label { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
    .value { margin-top:7px; font-size:18px; font-weight:750; overflow-wrap:anywhere; }
    .small { font-size:13px; }
    .vla { color:var(--vla); } .rlt { color:var(--rlt); } .human { color:var(--human); }
    .success { color:var(--ok); } .failure { color:var(--bad); } .hold { color:var(--hold); }
    .wide { grid-column:span 2; }
    footer { margin-top:12px; color:var(--muted); font-size:12px; }
    @media (max-width:900px) {
      .cameras { grid-template-columns:1fr; }
      .status { grid-template-columns:repeat(2,minmax(0,1fr)); }
      .wide { grid-column:span 2; }
    }
  </style>
</head>
<body><main>
  <header><h1>Seal Box · RLT Stage2 Monitor</h1><div class="live"><span id="dot" class="dot"></span><span id="fresh">WAITING</span></div></header>
  <section class="cameras">
    <figure><img id="cam-main" alt="main camera"><figcaption>MAIN / THIRD PERSON</figcaption></figure>
    <figure><img id="cam-left" alt="left wrist camera"><figcaption>LEFT WRIST</figcaption></figure>
    <figure><img id="cam-right" alt="right wrist camera"><figcaption>RIGHT WRIST</figcaption></figure>
  </section>
  <section class="status">
    <div class="card"><div class="label">Control mode</div><div id="mode" class="value">STARTING</div></div>
    <div class="card"><div class="label">RLT segment</div><div id="segment" class="value">INACTIVE</div></div>
    <div class="card"><div class="label">Last RLT label</div><div id="outcome" class="value">NOT YET</div></div>
    <div class="card"><div class="label">Server ack</div><div id="ack" class="value">—</div></div>
    <div class="card"><div class="label">Action source</div><div id="source" class="value small">—</div></div>
    <div class="card"><div class="label">VLA queue</div><div id="queue" class="value">0</div></div>
    <div class="card"><div class="label">Control rate</div><div id="hz" class="value">—</div></div>
    <div class="card"><div class="label">Last inference</div><div id="latency" class="value">—</div></div>
    <div class="card"><div class="label">Stage1 socket</div><div id="vla-conn" class="value">CLOSED</div></div>
    <div class="card"><div class="label">Stage2 socket</div><div id="rlt-conn" class="value">CLOSED</div></div>
    <div class="card wide"><div class="label">Stage2 episode / step</div><div id="episode" class="value small">—</div></div>
    <div class="card wide"><div class="label">Task</div><div id="task" class="value small">—</div></div>
    <div class="card wide"><div class="label">Last event / error</div><div id="event" class="value small">waiting for frames</div></div>
  </section>
  <footer>只读监控。SUCCESS / FAILURE 是操作员按 s / f 提交并由 Stage2 服务确认的 segment 标签，不是自动成功检测。</footer>
</main>
<script>
let lastSeq=-1;
const el=id=>document.getElementById(id);
const paint=(node,value)=>{ node.className='value'+(node.classList.contains('small')?' small':'');
  const v=String(value||'').toUpperCase();
  if(v.includes('VLA'))node.classList.add('vla'); if(v.includes('RLT'))node.classList.add('rlt');
  if(v.includes('HUMAN'))node.classList.add('human'); if(v.includes('SUCCESS')||v==='OPEN')node.classList.add('success');
  if(v.includes('FAIL')||v.includes('ERROR')||v==='CLOSED')node.classList.add('failure'); if(v.includes('HOLD'))node.classList.add('hold');
  node.textContent=value;
};
async function refresh(){ try {
  const r=await fetch('/api/status',{cache:'no-store'}); const s=await r.json();
  const age=(Date.now()/1000-s.updated_at_s); el('dot').classList.toggle('ok',age<2); el('fresh').textContent=age<2?'LIVE':`STALE ${age.toFixed(1)}s`;
  paint(el('mode'),s.control_mode); paint(el('segment'),s.rlt_segment_state); paint(el('outcome'),s.last_rlt_outcome);
  paint(el('ack'),s.last_rlt_ack); paint(el('source'),s.action_source); paint(el('queue'),String(s.vla_queue_size));
  paint(el('hz'),s.control_hz==null?'—':`${s.control_hz.toFixed(1)} Hz`); paint(el('latency'),s.inference_latency_ms==null?'—':`${s.inference_latency_ms.toFixed(1)} ms`);
  paint(el('vla-conn'),s.vla_connected?'OPEN':'CLOSED'); paint(el('rlt-conn'),s.stage2_connected?'OPEN':'CLOSED');
  paint(el('episode'),s.stage2_episode_id?`${s.stage2_episode_id.slice(0,10)}… / ${s.stage2_step}`:'—');
  paint(el('task'),s.task||'—'); paint(el('event'),s.last_error||s.last_event||'—');
  if(s.frame_seq!==lastSeq){ lastSeq=s.frame_seq; const q=`?v=${lastSeq}`;
    el('cam-main').src='/camera/main.jpg'+q; el('cam-left').src='/camera/left.jpg'+q; el('cam-right').src='/camera/right.jpg'+q; }
 } catch(e){ el('dot').classList.remove('ok'); el('fresh').textContent='DISCONNECTED'; paint(el('event'),String(e)); }}
setInterval(refresh,250); refresh();
</script></body></html>"""


class _MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RLTMonitorServer:
    """Read-only HTTP dashboard fed by frames already acquired by the control loop."""

    _CAMERA_ALIASES = {
        "main": "main",
        "cam_high": "main",
        "third_person": "main",
        "left_hand": "left",
        "left_wrist": "left",
        "cam_left_wrist": "left",
        "right_hand": "right",
        "right_wrist": "right",
        "cam_right_wrist": "right",
    }

    def __init__(
        self,
        *,
        host: str,
        port: int,
        image_fps: float,
        jpeg_quality: int,
    ) -> None:
        self._lock = threading.Lock()
        self._encode_event = threading.Event()
        self._stop_event = threading.Event()
        self._raw_frames: dict[str, np.ndarray] = {}
        self._jpeg_frames: dict[str, bytes] = {}
        self._raw_seq = 0
        self._last_publish_t = 0.0
        self._publish_interval_s = 1.0 / image_fps
        self._jpeg_quality = jpeg_quality
        now = time.time()
        self._state: dict[str, Any] = {
            "control_mode": "STARTING",
            "rlt_segment_state": "INACTIVE",
            "last_rlt_outcome": "NOT YET",
            "last_rlt_ack": "—",
            "action_source": "—",
            "vla_queue_size": 0,
            "control_hz": None,
            "inference_latency_ms": None,
            "vla_connected": False,
            "stage2_connected": False,
            "stage2_episode_id": None,
            "stage2_step": 0,
            "task": "",
            "last_event": "monitor started",
            "last_error": None,
            "frame_seq": 0,
            "updated_at_s": now,
        }
        self._httpd = _MonitorHTTPServer((host, port), self._handler_type())
        self.host = host
        self.port = int(self._httpd.server_address[1])
        self._encoder_thread = threading.Thread(
            target=self._encoder_loop,
            name="rlt-monitor-encoder",
            daemon=True,
        )
        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="rlt-monitor-http",
            daemon=True,
        )
        self._encoder_thread.start()
        self._server_thread.start()
        logging.info("RLT monitor listening on http://%s:%d", host, self.port)

    def _handler_type(self):
        monitor = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/":
                    self._send(HTTPStatus.OK, "text/html; charset=utf-8", _DASHBOARD_HTML.encode())
                    return
                if path == "/healthz":
                    self._send(HTTPStatus.OK, "text/plain; charset=utf-8", b"OK\n")
                    return
                if path == "/api/status":
                    body = json.dumps(monitor.snapshot(), ensure_ascii=False).encode()
                    self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
                    return
                if path.startswith("/camera/") and path.endswith(".jpg"):
                    camera = path.removeprefix("/camera/").removesuffix(".jpg")
                    frame = monitor.jpeg_frame(camera)
                    if frame is None:
                        self._send(HTTPStatus.SERVICE_UNAVAILABLE, "text/plain", b"frame unavailable\n")
                    else:
                        self._send(HTTPStatus.OK, "image/jpeg", frame)
                    return
                self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found\n")

            def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
                self.send_response(status.value)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                logging.debug("RLT monitor HTTP: " + fmt, *args)

        return Handler

    def update_status(self, **updates: Any) -> None:
        updates["updated_at_s"] = time.time()
        with self._lock:
            self._state.update(updates)

    def publish_observation(self, observation: dict[str, Any]) -> None:
        now = time.monotonic()
        if now - self._last_publish_t < self._publish_interval_s:
            self.update_status()
            return

        frames: dict[str, np.ndarray] = {}
        for key, value in observation.items():
            camera = self._camera_name(str(key))
            if camera is None:
                continue
            frame = self._to_rgb_hwc(value)
            if frame is not None:
                frames[camera] = frame.copy()
        if not frames:
            self.update_status()
            return

        self._last_publish_t = now
        with self._lock:
            self._raw_frames = frames
            self._raw_seq += 1
            self._state["updated_at_s"] = time.time()
        self._encode_event.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def jpeg_frame(self, camera: str) -> bytes | None:
        with self._lock:
            return self._jpeg_frames.get(camera)

    def close(self) -> None:
        self._stop_event.set()
        self._encode_event.set()
        self._httpd.shutdown()
        self._httpd.server_close()

    def _encoder_loop(self) -> None:
        encoded_seq = -1
        while not self._stop_event.is_set():
            self._encode_event.wait(0.5)
            self._encode_event.clear()
            with self._lock:
                seq = self._raw_seq
                frames = dict(self._raw_frames)
            if seq == encoded_seq:
                continue
            encoded: dict[str, bytes] = {}
            for name, rgb in frames.items():
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                ok, jpeg = cv2.imencode(
                    ".jpg",
                    bgr,
                    [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality],
                )
                if ok:
                    encoded[name] = jpeg.tobytes()
            with self._lock:
                self._jpeg_frames.update(encoded)
                self._state["frame_seq"] = seq
                self._state["updated_at_s"] = time.time()
            encoded_seq = seq

    @classmethod
    def _camera_name(cls, key: str) -> str | None:
        normalized = key.lower().replace("-", "_").replace(".", "_").replace("/", "_")
        for alias, target in cls._CAMERA_ALIASES.items():
            if normalized.endswith(alias):
                return target
        return None

    @staticmethod
    def _to_rgb_hwc(value: Any) -> np.ndarray | None:
        if hasattr(value, "detach"):
            value = value.detach().cpu().numpy()
        array = np.asarray(value)
        while array.ndim > 3 and array.shape[0] == 1:
            array = array[0]
        if array.ndim != 3:
            return None
        if array.shape[0] in (1, 3, 4) and array.shape[-1] not in (1, 3, 4):
            array = np.moveaxis(array, 0, -1)
        if array.shape[-1] == 1:
            array = np.repeat(array, 3, axis=-1)
        elif array.shape[-1] == 4:
            array = array[..., :3]
        if array.shape[-1] != 3:
            return None
        if np.issubdtype(array.dtype, np.floating):
            max_value = float(np.nanmax(array)) if array.size else 0.0
            scale = 255.0 if max_value <= 1.0 else 1.0
            array = np.clip(array * scale, 0, 255).astype(np.uint8)
        else:
            array = np.clip(array, 0, 255).astype(np.uint8, copy=False)
        return np.ascontiguousarray(array)

"""Named camera frames, independent source freshness, WebRTC and policy observations."""

from __future__ import annotations

import asyncio
import base64
import os
import threading
import time

import numpy as np


class Frames:
    def __init__(self):
        self.lock = threading.Lock()
        self.frames = {}
        self.tracks = {}
        self.encoding_lock = threading.Lock()
        self.observation_cache = {}

    def put(self, name, frame):
        with self.lock:
            self.frames[name] = (frame, time.monotonic())
            track = self.tracks.get(name)
        if track:
            track.push_frame(frame)

    def snapshot(self):
        with self.lock:
            return dict(self.frames)

    def healthy(self, cfg):
        frames = self.snapshot()
        return all(
            c.name in frames
            and frames[c.name][0].shape == c.output_shape
            and time.monotonic() - frames[c.name][1] <= cfg.safety.camera_max_age_ms / 1000
            for c in cfg.cameras
        )

    def jpeg(self, name, quality=80, size=None):
        import cv2

        value = self.snapshot().get(name)
        if value is None:
            return None
        frame = value[0]
        if size:
            frame = cv2.resize(frame, size)
        ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return data.tobytes() if ok else None

    def observation_images(self):
        import cv2

        # A 100 Hz state transport must not re-encode the same camera exposure
        # between capture frames. Source timestamps/freshness are unchanged.
        with self.encoding_lock:
            result = {}
            for name, (frame, stamp) in self.snapshot().items():
                cached = self.observation_cache.get(name)
                if cached is None or cached[0] != stamp:
                    resized = cv2.resize(frame, (320, 240))
                    ok, data = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if not ok:
                        raise ValueError("INVALID_POLICY_IMAGE")
                    cached = (stamp, base64.b64encode(data.tobytes()).decode())
                    self.observation_cache[name] = cached
                result[name] = cached[1]
            return result


class Cameras:
    def __init__(self, cfg, frames, ros_io=None):
        self.cfg, self.frames = cfg, frames
        self.captures = []
        self.stop_event = threading.Event()
        self.thread = None
        for c in cfg.cameras:
            if c.backend == "usb":
                from .adapters.uvc import UsbCameraCapture, UsbCameraConfig

                camera = UsbCameraCapture(
                    UsbCameraConfig(
                        stream_id=c.name,
                        name=c.name,
                        topic=c.topic,
                        device=c.device,
                        width=c.width,
                        height=c.height,
                        fps=c.fps,
                        rotation=c.rotation,
                        fourcc=c.fourcc,
                        buffer_size=c.buffer_size,
                        warmup_frames=c.warmup_frames,
                        configure_v4l2_format=c.configure_v4l2,
                        source_timeout_seconds=cfg.safety.camera_max_age_ms / 1000,
                    ),
                    lambda frame, name=c.name: frames.put(name, frame),
                )
                camera.start()
                self.captures.append(camera)
            elif c.backend == "ros":
                from sensor_msgs.msg import Image

                def callback(msg, camera=c):
                    import cv2

                    if msg.encoding not in ("rgb8", "bgr8") or msg.step < msg.width * 3:
                        return
                    raw = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.step)
                    frame = raw[:, : msg.width * 3].reshape(msg.height, msg.width, 3).copy()
                    if msg.encoding == "rgb8":
                        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    from .adapters.uvc import _rotate_frame

                    frames.put(camera.name, _rotate_frame(frame, camera.rotation))

                ros_io.node.create_subscription(Image, c.topic, callback, ros_io.qos)
        if cfg.general.simulation:
            self.thread = threading.Thread(target=self._simulate, daemon=True)
            self.thread.start()

    def _simulate(self):
        import cv2

        started = time.monotonic()
        while not self.stop_event.is_set():
            elapsed = time.monotonic() - started
            for i, c in enumerate(self.cfg.cameras):
                frame = np.full((c.height, c.width, 3), (35 + i * 10, 28, 22), dtype=np.uint8)
                cv2.putText(frame, c.name.upper(), (24, 44), cv2.FONT_HERSHEY_SIMPLEX, 1, (220, 225, 230), 2)
                cv2.putText(
                    frame,
                    f"SIMULATION  {elapsed:08.2f}s",
                    (24, c.height - 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (150, 190, 180),
                    1,
                )
                cv2.circle(
                    frame,
                    (int(c.width / 2 + 100 * np.sin(elapsed + i)), c.height // 2),
                    35,
                    (90, 180, 80),
                    -1,
                )
                if c.rotation in (90, 270):
                    frame = cv2.rotate(
                        frame, cv2.ROTATE_90_CLOCKWISE if c.rotation == 90 else cv2.ROTATE_90_COUNTERCLOCKWISE
                    )
                self.frames.put(c.name, frame)
            self.stop_event.wait(1 / max(c.fps for c in self.cfg.cameras))

    def close(self):
        self.stop_event.set()
        for capture in self.captures:
            capture.stop()
        if self.thread:
            self.thread.join(timeout=2)


class VideoPeer:
    def __init__(self, cfg, role, frames, send):
        self.cfg, self.role, self.frames, self.send = cfg, role, frames, send
        self.pc = None
        self.tasks = set()
        self.generation = None
        self.negotiation = asyncio.Lock()

    async def _new(self):
        await self.close()
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection

        ice = [
            RTCIceServer(
                urls=url,
                username=os.getenv(self.cfg.network.turn_username_env),
                credential=os.getenv(self.cfg.network.turn_password_env),
            )
            for url in self.cfg.network.ice_servers
        ]
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=ice))
        if self.role == "client":
            names = iter(c.name for c in self.cfg.cameras)

            @self.pc.on("track")
            def track_received(track):
                name = next(names, None)
                if name:
                    task = asyncio.create_task(self._consume(name, track))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)

    async def offer(self):
        if not self.cfg.network.webrtc_enabled:
            return
        async with self.negotiation:
            import uuid
            from .adapters.uvc import CameraVideoTrack

            await self._new()
            self.generation = uuid.uuid4().hex
            for c in self.cfg.cameras:
                track = CameraVideoTrack(c.fps)
                track.set_loop(asyncio.get_running_loop())
                with self.frames.lock:
                    self.frames.tracks[c.name] = track
                self.pc.addTrack(track)
            await self.pc.setLocalDescription(await self.pc.createOffer())
            self.send(
                {
                    "type": "signal",
                    "description": "offer",
                    "sdp": self.pc.localDescription.sdp,
                    "generation": self.generation,
                    "cameras": [c.name for c in self.cfg.cameras],
                }
            )

    async def receive(self, msg):
        if not self.cfg.network.webrtc_enabled:
            return
        from aiortc import RTCSessionDescription

        async with self.negotiation:
            if msg["description"] == "offer" and self.role == "client":
                if msg.get("cameras") != [c.name for c in self.cfg.cameras]:
                    raise ValueError("CAMERA_CONTRACT_MISMATCH")
                await self._new()
                self.generation = msg["generation"]
                await self.pc.setRemoteDescription(RTCSessionDescription(msg["sdp"], "offer"))
                await self.pc.setLocalDescription(await self.pc.createAnswer())
                self.send(
                    {
                        "type": "signal",
                        "description": "answer",
                        "sdp": self.pc.localDescription.sdp,
                        "generation": self.generation,
                    }
                )
            elif msg["description"] == "answer" and self.pc and msg.get("generation") == self.generation:
                await self.pc.setRemoteDescription(RTCSessionDescription(msg["sdp"], "answer"))

    async def _consume(self, name, track):
        try:
            while True:
                frame = await track.recv()
                self.frames.put(name, frame.to_ndarray(format="bgr24"))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Freshness expires; do not synthesize replacement frames.
            return

    async def close(self):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        with self.frames.lock:
            self.frames.tracks.clear()
        if self.pc:
            await self.pc.close()
            self.pc = None

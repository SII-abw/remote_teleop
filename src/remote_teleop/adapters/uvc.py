"""UVC capture and WebRTC track extracted from the existing Python platform.
Source: reomte_controller/slave_client.py, working tree based on dd67396.
Modified: per-camera source deadline; identical pixels are not proof of a stale buffer.
No legacy entry points, credentials, control code or configuration are imported.
"""

from __future__ import annotations
import asyncio
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, Optional
import cv2
import numpy as np
from aiortc import VideoStreamTrack
from av import VideoFrame

logger = logging.getLogger(__name__)
USB_CAMERA_BACKENDS = {"v4l2": cv2.CAP_V4L2, "any": cv2.CAP_ANY}
VIDEO_CLOCK_RATE = 90000
CAMERA_FROZEN_FRAME_TIMEOUT_SEC = float("inf")


@dataclass
class UsbCameraConfig:
    stream_id: str
    topic: str
    name: str
    device: str
    width: int = 640
    height: int = 480
    fps: float = 60.0
    fourcc: str = "MJPG"
    rotation: float = 0.0
    backend: str = "v4l2"
    buffer_size: int = 1
    warmup_frames: int = 10
    reopen_on_error: bool = True
    retry_interval_sec: float = 2.0
    configure_v4l2_format: bool = True
    diagnostics_interval_sec: float = 0.0
    source_timeout_seconds: float = 0.3


def _fourcc_to_string(value: float) -> str:
    code = int(value)
    return "".join(chr((code >> 8 * i) & 0xFF) for i in range(4)).strip()


def _opencv_device(device: str) -> int | str:
    text = str(device).strip()
    if text.isdigit():
        return int(text)
    if text.startswith("/dev/video") and text[len("/dev/video") :].isdigit():
        return int(text[len("/dev/video") :])
    return text


def _v4l2_device(device: str) -> str:
    value = _opencv_device(device)
    if isinstance(value, int):
        return f"/dev/video{value}"
    return str(value)


def _rotate_frame(frame: np.ndarray, degrees: float) -> np.ndarray:
    normalized = int(degrees) % 360
    if normalized == 0:
        return frame
    if normalized == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if normalized == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if normalized == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

    height, width = frame.shape[:2]
    center = (width / 2.0, height / 2.0)
    transform = cv2.getRotationMatrix2D(center, -degrees, 1.0)
    return cv2.warpAffine(frame, transform, (width, height))


def _run_v4l2_ctl(device: str, args: list[str]) -> Optional[subprocess.CompletedProcess[str]]:
    if shutil.which("v4l2-ctl") is None:
        logger.info("v4l2-ctl is not installed; relying on OpenCV camera setup.")
        return None
    try:
        return subprocess.run(
            ["v4l2-ctl", "-d", device, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("v4l2-ctl failed for %s: %s", device, exc)
        return None


def _configure_v4l2_capture_mode(config: UsbCameraConfig) -> None:
    if not config.configure_v4l2_format or config.backend != "v4l2":
        return

    device = _v4l2_device(config.device)
    format_parts = [f"width={config.width}", f"height={config.height}"]
    if config.fourcc:
        format_parts.append(f"pixelformat={config.fourcc}")

    format_result = _run_v4l2_ctl(
        device,
        [f"--set-fmt-video={','.join(format_parts)}"],
    )
    if format_result is not None and format_result.returncode != 0:
        detail = (format_result.stderr or format_result.stdout).strip()
        logger.warning(
            "%s: v4l2-ctl failed to set capture format for %s: %s",
            config.stream_id,
            config.device,
            detail,
        )

    fps_text = f"{int(config.fps)}" if float(config.fps).is_integer() else f"{config.fps:g}"
    fps_result = _run_v4l2_ctl(device, [f"--set-parm={fps_text}"])
    if fps_result is not None and fps_result.returncode != 0:
        detail = (fps_result.stderr or fps_result.stdout).strip()
        logger.warning(
            "%s: v4l2-ctl failed to set FPS %s for %s: %s",
            config.stream_id,
            fps_text,
            config.device,
            detail,
        )


class UsbCameraCapture:
    """Continuously reads one UVC camera and calls back with the latest BGR frame."""

    def __init__(self, config: UsbCameraConfig, on_frame):
        self.config = config
        self._backend_id = USB_CAMERA_BACKENDS.get(config.backend)
        if self._backend_id is None:
            raise ValueError(
                f"Unsupported USB camera backend {config.backend!r}. "
                f"Use one of: {sorted(USB_CAMERA_BACKENDS)}"
            )
        self._on_frame = on_frame
        self._cap = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._next_open_attempt = 0.0
        self._latest_stamp = 0.0
        self._health_lock = threading.Lock()
        self._capture_open = False
        self._healthy = False
        self._health_reason = "Waiting for first camera frame"
        self._last_error: Optional[str] = None
        self._last_failure_stamp = 0.0
        self._consecutive_failures = 0
        self._last_frame_sample: Optional[np.ndarray] = None
        self._last_frame_change_stamp = 0.0

        self._diag_started_at = time.monotonic()
        self._diag_frames = 0
        self._diag_read_time = 0.0
        self._diag_loop_time = 0.0
        self._diag_max_loop_time = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"usbcam-{self.config.stream_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._release_capture()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._cap is None:
                if time.monotonic() < self._next_open_attempt:
                    time.sleep(0.02)
                    continue
                self._open_capture()
                if self._cap is None:
                    continue

            loop_started_at = time.monotonic()
            read_started_at = time.monotonic()
            read_error = None
            try:
                ok, frame = self._cap.read()
            except Exception as exc:
                ok, frame = False, None
                read_error = f"OpenCV read raised {type(exc).__name__}: {exc}"
            read_time = time.monotonic() - read_started_at
            if not ok or frame is None:
                self._mark_failure(read_error or "OpenCV returned no frame")
                logger.warning(
                    "%s: failed to read frame from %s.",
                    self.config.stream_id,
                    self.config.device,
                )
                if self.config.reopen_on_error:
                    self._release_capture()
                    self._next_open_attempt = time.monotonic() + self.config.retry_interval_sec
                continue

            try:
                frame = _rotate_frame(frame, self.config.rotation)
                if not frame.flags["C_CONTIGUOUS"]:
                    frame = np.ascontiguousarray(frame)
                self._mark_frame_received(frame)
            except Exception as exc:
                self._mark_failure(f"Camera frame processing raised {type(exc).__name__}: {exc}")
                logger.warning(
                    "%s: camera frame processing failed: %s",
                    self.config.stream_id,
                    exc,
                )
                if self.config.reopen_on_error:
                    self._release_capture()
                    self._next_open_attempt = time.monotonic() + self.config.retry_interval_sec
                continue

            try:
                self._on_frame(frame)
            except Exception as exc:
                self._mark_failure(f"Camera frame callback raised {type(exc).__name__}: {exc}")
                logger.warning(
                    "%s: camera frame callback failed: %s",
                    self.config.stream_id,
                    exc,
                )

            self._update_diagnostics(read_time, time.monotonic() - loop_started_at)

    def _open_capture(self) -> None:
        self._release_capture()
        self._next_open_attempt = time.monotonic() + self.config.retry_interval_sec
        _configure_v4l2_capture_mode(self.config)

        try:
            cap = cv2.VideoCapture(
                _opencv_device(self.config.device),
                self._backend_id,
            )
        except Exception as exc:
            self._mark_failure(f"OpenCV VideoCapture raised {type(exc).__name__}: {exc}")
            logger.error(
                "%s: failed to create USB camera capture for %s: %s",
                self.config.stream_id,
                self.config.device,
                exc,
            )
            return
        if not cap.isOpened():
            cap.release()
            self._mark_failure("OpenCV could not open camera device")
            logger.error(
                "%s: could not open USB camera %s.",
                self.config.stream_id,
                self.config.device,
            )
            return

        if self.config.buffer_size > 0:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, self.config.buffer_size)
        if self.config.fourcc and len(self.config.fourcc) == 4:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.config.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        cap.set(cv2.CAP_PROP_FPS, self.config.fps)

        for _ in range(max(self.config.warmup_frames, 0)):
            ok, _ = cap.read()
            if not ok:
                break

        self._cap = cap
        with self._health_lock:
            self._capture_open = True
        actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        actual_fourcc = _fourcc_to_string(cap.get(cv2.CAP_PROP_FOURCC)) or "unknown"
        logger.info(
            "%s: opened USB camera %s (%s) for topic %s; requested %dx%d@%g %s, "
            "actual %dx%d@%.1f %s, rotation=%g",
            self.config.stream_id,
            self.config.name,
            self.config.device,
            self.config.topic,
            self.config.width,
            self.config.height,
            self.config.fps,
            self.config.fourcc or "auto",
            actual_width,
            actual_height,
            actual_fps,
            actual_fourcc,
            self.config.rotation,
        )

    def _release_capture(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        with self._health_lock:
            self._capture_open = False

    def _mark_frame_received(self, frame: np.ndarray) -> None:
        """Update source freshness and detect a UVC driver replaying one buffer."""
        now = time.monotonic()
        # Small exact sample keeps the check inexpensive and avoids comparing
        # full-resolution arrays at the capture FPS.
        sample = cv2.resize(frame, (64, 48), interpolation=cv2.INTER_AREA)
        with self._health_lock:
            self._latest_stamp = now
            changed = self._last_frame_sample is None or not np.array_equal(sample, self._last_frame_sample)
            if changed:
                was_healthy = self._healthy
                self._last_frame_sample = sample.copy()
                self._last_frame_change_stamp = now
                self._healthy = True
                self._health_reason = "OK"
                self._last_error = None
                self._consecutive_failures = 0
                if not was_healthy:
                    logger.info(
                        "%s: USB camera source is healthy again.",
                        self.config.stream_id,
                    )
                return

            frozen_for = now - self._last_frame_change_stamp
            if frozen_for >= CAMERA_FROZEN_FRAME_TIMEOUT_SEC:
                self._healthy = False
                self._health_reason = f"Camera source is replaying an identical frame for {frozen_for:.1f}s"
                self._last_error = self._health_reason
                self._last_failure_stamp = now
                self._consecutive_failures = max(self._consecutive_failures, 1)

    def _mark_failure(self, reason: str) -> None:
        now = time.monotonic()
        with self._health_lock:
            self._healthy = False
            self._health_reason = reason
            self._last_error = reason
            self._last_failure_stamp = now
            self._consecutive_failures += 1

    def health_status(self) -> Dict[str, Any]:
        """Thread-safe source-camera health for safety and heartbeat reporting."""
        now = time.monotonic()
        with self._health_lock:
            age_ms = None if self._latest_stamp <= 0.0 else (now - self._latest_stamp) * 1000.0
            healthy = self._healthy
            reason = self._health_reason
            if self._latest_stamp <= 0.0:
                healthy = False
                reason = "No source frame has been captured"
            elif age_ms is not None and age_ms > self.config.source_timeout_seconds * 1000.0:
                healthy = False
                reason = f"No fresh source frame for {age_ms:.0f}ms"

            return {
                "stream_id": self.config.stream_id,
                "name": self.config.name,
                "device": self.config.device,
                "healthy": healthy,
                "opened": self._capture_open,
                "age_ms": round(age_ms, 1) if age_ms is not None else None,
                "reason": reason,
                "last_error": self._last_error,
                "consecutive_failures": self._consecutive_failures,
            }

    def _update_diagnostics(self, read_time: float, loop_time: float) -> None:
        interval = self.config.diagnostics_interval_sec
        if interval <= 0:
            return

        self._diag_frames += 1
        self._diag_read_time += read_time
        self._diag_loop_time += loop_time
        self._diag_max_loop_time = max(self._diag_max_loop_time, loop_time)

        now = time.monotonic()
        elapsed = now - self._diag_started_at
        if elapsed < interval:
            return

        frames = max(self._diag_frames, 1)
        logger.info(
            "%s debug: real USB read FPS=%.1f, avg_read=%.2f ms, "
            "avg_loop=%.2f ms, max_loop=%.2f ms, age=%.1f ms",
            self.config.stream_id,
            self._diag_frames / elapsed,
            self._diag_read_time / frames * 1e3,
            self._diag_loop_time / frames * 1e3,
            self._diag_max_loop_time * 1e3,
            (time.monotonic() - self._latest_stamp) * 1e3 if self._latest_stamp else -1.0,
        )
        self._diag_started_at = now
        self._diag_frames = 0
        self._diag_read_time = 0.0
        self._diag_loop_time = 0.0
        self._diag_max_loop_time = 0.0


class CameraVideoTrack(VideoStreamTrack):
    """
    将 USB camera 最新 OpenCV BGR 帧包装为 aiortc VideoStreamTrack，
    供 WebRTC PeerConnection 作为视频轨道推流。
    """

    kind = "video"

    def __init__(self, fps: float = 60.0):
        super().__init__()
        self._frame: Optional[np.ndarray] = None
        self._frame_event = asyncio.Event()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clock_start: Optional[float] = None
        self._last_pts = -1
        self._fps = max(float(fps), 1.0)
        self._time_base = Fraction(1, VIDEO_CLOCK_RATE)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def push_frame(self, cv_image: np.ndarray) -> None:
        """从 USB 采集线程推入新的 OpenCV BGR 帧。"""
        with self._lock:
            self._frame = cv_image
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._frame_event.set)

    async def recv(self) -> VideoFrame:
        """aiortc 获取下一张由相机采集线程实际推入的帧。

        Never synthesize black frames or resend a cached frame here.  Doing so
        makes the receiver believe that WebRTC is healthy after the UVC source
        has stopped, which is precisely the silent-failure mode this safety
        path must avoid.
        """
        while True:
            await self._frame_event.wait()
            self._frame_event.clear()
            with self._lock:
                if self._frame is not None:
                    cv_image = self._frame
                    break

        frame = VideoFrame.from_ndarray(cv_image, format="bgr24")
        now = time.monotonic()
        if self._clock_start is None:
            self._clock_start = now
            pts = 0
        else:
            pts = int((now - self._clock_start) * VIDEO_CLOCK_RATE)
            if pts <= self._last_pts:
                pts = self._last_pts + 1
        self._last_pts = pts

        frame.pts = pts
        frame.time_base = self._time_base
        return frame

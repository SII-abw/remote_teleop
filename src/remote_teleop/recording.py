"""Bounded asynchronous v3 dataset writer, preserving the HIL dataset contract."""

import dataclasses
import json
import queue
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

import numpy as np

from .config import atomic_json
from .datasets import hil_features
from .task_sessions import TaskSessions


class Recorder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=cfg.recording.queue_size)
        self.state = "IDLE"
        self.error = None
        self.count = 0
        self.dataset = None
        self.thread = None
        self.stop_event = threading.Event()
        self.path = None
        self.started = 0.0
        self.task_sessions = TaskSessions(cfg.recording.root, cfg.general.task)
        self.record_task = None

    def automatic_name_status(self):
        session = self.task_sessions.preview()
        return {"name": session["next_dataset_name"], "valid": True, "available": True,
                "error": None, "suggested_name": None, "task_session": session}

    def name_status(self, name):
        """Read-only name check; suggestions never reserve or overwrite data."""
        valid = (
            isinstance(name, str)
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name))
            and ".." not in name
        )
        root = Path(self.cfg.recording.root).resolve()

        def occupied(candidate):
            path = root / candidate
            return path.exists() or path.is_symlink()

        available = valid and not occupied(name)
        suggestion = name if available else None
        if not available:
            base = re.sub(r"_\d{3}$", "", name) if valid else "episode"
            for number in range(1, 1000):
                candidate = f"{base[:76]}_{number:03d}"
                if not occupied(candidate):
                    suggestion = candidate
                    break
            if suggestion is None:
                suggestion = f"{base[:71]}_{uuid.uuid4().hex[:8]}"
        return {
            "name": name,
            "valid": valid,
            "available": available,
            "suggested_name": suggestion,
            "error": None if available else "DATASET_EXISTS" if valid else "INVALID_DATASET_NAME",
        }

    def start(self, name, shapes):
        with self.lock:
            if self.state != "IDLE":
                raise ValueError("RECORDER_BUSY")
            if name is None:
                reservation = self.task_sessions.reserve_episode()
                name, self.record_task = reservation["name"], reservation["task"]
            else:
                self.record_task = self.task_sessions.snapshot()["active"]
            checked = self.name_status(name)
            if not checked["valid"]:
                raise ValueError("INVALID_DATASET_NAME")
            root = Path(self.cfg.recording.root).resolve()
            root.mkdir(parents=True, exist_ok=True)
            path = root / name
            if path.exists() or path.is_symlink():
                raise ValueError("DATASET_EXISTS")
            if shutil.disk_usage(root).free < self.cfg.recording.minimum_free_gb * 1e9:
                raise ValueError("DISK_SPACE_LOW")
            self.state = "STARTING"
            try:
                from remote_teleop.components.datasets.lerobot_dataset import LeRobotDataset

                self.dataset = LeRobotDataset.create(
                    self.cfg.recording.repo_id,
                    self.cfg.recording.fps,
                    root=path,
                    robot_type="ros_follower",
                    features=hil_features(self.cfg.joint_names, shapes),
                    use_videos=True,
                    vcodec=self.cfg.recording.codec,
                    image_writer_processes=0,
                    image_writer_threads=4,
                    batch_encoding_size=1,
                    separate_episode_files=True,
                )
                self.path = path
                atomic_json(
                    path / "audit/incomplete.json", {"reason": "recording_open", "started_at": time.time()}
                )
                atomic_json(
                    path / "audit/contract.json",
                    {
                        "schema": "hil_v1",
                        "action_semantics": "applied_command_v1",
                        "operation_mode": self.cfg.general.operation_mode,
                        "policy_id": self.cfg.policy.policy_id,
                        "joint_units": ["rad"] * 6 + ["m"],
                        "simulation": self.cfg.general.simulation,
                        "task": self.record_task,
                    },
                )
                self.stop_event.clear()
                self.queue = queue.Queue(maxsize=self.cfg.recording.queue_size)
                self.count, self.error, self.started = 0, None, time.monotonic()
                self.state = "RECORDING"
                self.thread = threading.Thread(target=self._write, daemon=True, name="dataset-writer")
                self.thread.start()
            except Exception:
                self.state = "ERROR"
                self.error = "RECORDER_START_FAILED"
                if path.exists():
                    atomic_json(path / "audit/incomplete.json", {"reason": self.error, "time": time.time()})
                raise

    def submit(self, measured, applied, frames, observation_id):
        if self.state != "RECORDING":
            return
        now = time.monotonic()
        if now - self.started > self.cfg.recording.max_episode_seconds:
            self.fail("EPISODE_TIME_LIMIT")
            return
        if len(frames) != 3 or any(
            now - stamp > self.cfg.safety.camera_max_age_ms / 1000 for _, stamp in frames.values()
        ):
            self.fail("RECORDING_CAMERA_STALE")
            return
        if (
            max(s for _, s in frames.values()) - min(s for _, s in frames.values())
            > self.cfg.recording.max_sync_skew_ms / 1000
        ):
            self.fail("RECORDING_SYNC_SKEW")
            return
        item = (list(measured), applied, dict(frames), observation_id, now)
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            self.fail("RECORDING_QUEUE_FULL")

    def fail(self, code):
        self.error, self.state = code, "ERROR"
        self.stop_event.set()

    def _write(self):
        try:
            import cv2

            with (self.path / "audit/frames.jsonl").open("a") as audit:
                while not self.stop_event.is_set() or not self.queue.empty():
                    try:
                        q, applied, images, observation_id, stamp = self.queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if (
                        self.count % self.cfg.recording.fps == 0
                        and shutil.disk_usage(self.path).free < self.cfg.recording.minimum_free_gb * 1e9
                    ):
                        self.fail("DISK_SPACE_LOW")
                        return
                    frame = {
                        "observation.state": np.asarray(q, dtype=np.float32),
                        "action": np.asarray(applied.action, dtype=np.float32),
                        "complementary_info.policy_action": np.asarray(
                            applied.policy_action, dtype=np.float32
                        ),
                        "complementary_info.is_intervention": np.array(
                            applied.intervention, dtype=np.float32
                        ),
                        "complementary_info.state": np.array(applied.hil_state, dtype=np.float32),
                        "complementary_info.collector_policy_id": applied.source,
                        "task": self.record_task["prompt"],
                    }
                    for name, (image, _) in images.items():
                        frame[f"observation.images.{name}"] = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                    self.dataset.add_frame(frame)
                    audit.write(
                        json.dumps(
                            {
                                "frame_index": self.count,
                                "monotonic": stamp,
                                "recording_observation_id": observation_id,
                                "measured": q,
                                "camera_stamps": {n: s for n, (_, s) in images.items()},
                                **dataclasses.asdict(applied),
                            },
                            allow_nan=False,
                        )
                        + "\n"
                    )
                    self.count += 1
                    if self.count % self.cfg.recording.fps == 0:
                        audit.flush()
        except Exception as exc:
            self.fail("WRITE_FAILED_" + type(exc).__name__)

    def finish(self, outcome):
        if outcome not in ("success", "failure", "discard"):
            raise ValueError("INVALID_OUTCOME")
        with self.lock:
            if self.dataset is None:
                if outcome == "discard" and self.state == "ERROR":
                    self.state, self.error = "IDLE", None
                    return self.status()
                raise ValueError("NOT_RECORDING")
            self.stop_event.set()
            if self.thread:
                self.thread.join(timeout=10)
            if self.thread and self.thread.is_alive():
                self.fail("WRITER_STOP_TIMEOUT")
                raise ValueError("WRITER_STOP_TIMEOUT")
            failed = self.state == "ERROR"
            self.state = "SAVING"
            try:
                if outcome == "discard":
                    self.dataset.clear_episode_buffer()
                elif failed or self.count == 0:
                    raise ValueError("INCOMPLETE_EPISODE")
                else:
                    self.dataset.save_episode(extra_episode_metadata={"episode_success": outcome})
                finalize = getattr(self.dataset, "finalize", None)
                if finalize:
                    finalize()
                self.dataset.stop_image_writer()
                atomic_json(
                    self.path / "audit/outcome.json",
                    {"outcome": outcome, "frames": self.count, "error": self.error},
                )
                (self.path / "audit/incomplete.json").unlink(missing_ok=True)
                self.dataset = None
                self.state = "IDLE"
            except Exception:
                self.state = "ERROR"
                raise
        return self.status()

    def status(self):
        return {
            "state": self.state,
            "error": self.error,
            "frames": self.count,
            "queued": self.queue.qsize(),
            "dataset": self.path.name if self.path else None,
        }

    def close(self):
        if self.dataset is not None:
            self.fail("PROCESS_SHUTDOWN")
            if self.thread:
                self.thread.join(timeout=3)
            # Keep incomplete marker and source images; never label a crash as success.

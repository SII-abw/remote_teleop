"""Role runtimes. Robot motion, network, recording and inference have separate owners."""

from __future__ import annotations

import asyncio
import base64
import logging
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4

from .adapters.hardware import leader_adapter, robot_adapter
from .control import Controller, Mode, Rejected, alignment_report, vector
from .homing import HomingSession
from .grippers import GripperTakeover
from .datasets import DatasetInspector
from .media import Cameras, Frames, VideoPeer
from .policy import PolicyRunner
from .recording import Recorder
from .transport import Endpoint

logger = logging.getLogger(__name__)


class RobotRuntime:
    def __init__(self, store):
        self.store, self.cfg = store, store.current
        self.control = Controller(self.cfg)
        self.lock = threading.RLock()
        self.operation_lock = asyncio.Lock()
        self.stop_event = threading.Event()
        self.frames = Frames()
        self.recorder = Recorder(self.cfg)
        self.inspector = DatasetInspector(self.cfg)
        self.endpoint = Endpoint(self.cfg, "robot", self.on_message, self.disconnected)
        self.video = VideoPeer(self.cfg, "robot", self.frames, self.endpoint.send)
        self.driver = None
        self.cameras = None
        self.runner = None
        self.last_loop = time.monotonic()
        self.network_task = None
        self.rpc_cache = OrderedDict()
        self.tasks = set()
        self.rejected = 0
        self.last_rejection = None
        self.current_observation_id = ""
        self.applied = None
        self.applied_epoch = None
        self.video_task = None
        self.supervisor_fault = threading.Event()
        self.record_job = None
        self.record_job_status = None
        self.homing = None
        self.homing_status = None
        self.home_operator_at = 0.0
        self.home_operator_ok = False
        self.leader_home_report = None
        self.audit_path = (
            Path(self.cfg.recording.root).resolve().parent / "state" / "audit" / f"robot-{uuid4().hex}.jsonl"
        )

    async def start(self):
        self.driver = robot_adapter(self.cfg)
        self.cameras = Cameras(self.cfg, self.frames, getattr(self.driver, "io", None))
        self.thread = threading.Thread(target=self._control_loop, daemon=True, name="robot-control")
        self.watchdog = threading.Thread(target=self._supervise, daemon=True, name="robot-supervisor")
        self.thread.start()
        self.watchdog.start()
        if self.cfg.policy.runner_location == "robot":
            self.runner = PolicyRunner(self.cfg, "robot", lambda msg: self._offer(msg, "robot"))
            self.runner.start()
        await self.endpoint.start()
        self.network_task = asyncio.create_task(self._publish())

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def disconnected(self):
        with self.lock:
            self.control.hold("TRANSPORT_LOST")
            self._end_homing("TRANSPORT_LOST", failed=True)
        if self.driver:
            self.driver.stop()

    def health(self, stamp):
        return (
            not self.cfg.safety.checks.state_freshness
            or time.monotonic() - stamp <= self.cfg.safety.state_max_age_ms / 1000
        ) and (not self.cfg.safety.require_cameras or self.frames.healthy(self.cfg))

    def _offer(self, packet, source):
        try:
            with self.lock:
                self.control.offer(packet, source)
        except (ValueError, KeyError) as exc:
            with self.lock:
                self.rejected += 1
                self.last_rejection = str(exc)[:160]
                now = time.monotonic()
                observed = self.control.observations.get(packet.get("observation_id"))
                self.control.audit.append({
                    "event": "command_rejected", "reason": self.last_rejection,
                    "message_type": packet.get("type"), "seq": packet.get("seq"),
                    "epoch": packet.get("epoch"), "monotonic": now,
                    "observation_age_ms": (now - observed) * 1000 if observed is not None else None,
                })
                self.control.audit = self.control.audit[-1000:]

    async def on_message(self, msg):
        kind = msg["type"]
        if kind in ("human_action", "policy_action", "policy_request", "policy_error"):
            self._offer(msg, msg["source"])
        elif kind == "leader_heartbeat":
            with self.lock:
                self.control.heartbeat(msg.get("healthy") is True, msg.get("transition"))
                self.home_operator_at = time.monotonic()
                self.home_operator_ok = msg.get("operator_healthy") is True
                report = msg.get("homing")
                if (
                    isinstance(report, dict)
                    and self.homing_status
                    and report.get("id") == self.homing_status["id"]
                ):
                    self.leader_home_report = {**report, "received_at": time.monotonic()}
        elif kind == "mode_ready":
            try:
                with self.lock:
                    self.control.mode_ready(msg["epoch"], msg["pose"])
            except ValueError as exc:
                self.last_rejection = str(exc)
                with self.lock:
                    self.control.hold("LEADER_PREPARE_FAILED")
        elif kind == "config_mismatch":
            with self.lock:
                self.control.hold("CONFIG_NOT_SYNCHRONIZED")
                self._end_homing("CONFIG_NOT_SYNCHRONIZED", failed=True)
            self.driver.stop()
        elif kind == "stop":
            with self.lock:
                if self.control.mode != Mode.ESTOP:
                    self.control._transition(Mode.ESTOP, "REMOTE_STOP")
                self._end_homing("REMOTE_STOP", failed=True)
            self.driver.stop()
        elif kind == "rpc_request":
            if len(self.tasks) >= 32:
                self.endpoint.send(
                    {"type": "rpc_response", "id": msg.get("id"), "ok": False, "error": "REQUEST_LIMIT"}
                )
            else:
                self._spawn(self._rpc(msg))
        elif kind == "peer":
            self.disconnected()
            if msg.get("connected"):
                if self.video_task:
                    self.video_task.cancel()
                self.video_task = asyncio.create_task(self.video.offer())
        elif kind == "signal":
            self._spawn(self.video.receive(msg))

    async def _rpc(self, request):
        async with self.operation_lock:
            identifier = request.get("id")
            if not isinstance(identifier, str) or len(identifier) > 64:
                return
            if identifier in self.rpc_cache:
                self.endpoint.send(self.rpc_cache[identifier])
                return
            try:
                if request["operation"] in ("record_start", "record_finish"):
                    if self.record_job and not self.record_job.done():
                        raise ValueError("RECORDER_BUSY")
                    self.record_job_status = {
                        "id": identifier, "state": "QUEUED", "error": None,
                        "operation": request["operation"],
                        "dataset": request.get("data", {}).get("name")
                        if request["operation"] == "record_start" else self.recorder.status().get("dataset"),
                    }
                    if request["operation"] == "record_finish":
                        with self.lock:
                            self.control.hold("EPISODE_FINISHED")
                            self.control.recording = False
                        self.driver.stop()
                    self.record_job = asyncio.create_task(self._record_job(request))
                    result = dict(self.record_job_status)
                else:
                    result = await asyncio.to_thread(
                        self._operation, request["operation"], request.get("data", {})
                    )
                response = {"type": "rpc_response", "id": identifier, "ok": True, "result": result}
            except Exception as exc:
                logger.warning("Robot operation %s failed (%s)", request.get("operation"), type(exc).__name__)
                response = {"type": "rpc_response", "id": identifier, "ok": False, "error": str(exc)[:1000]}
            self.rpc_cache[identifier] = response
            while len(self.rpc_cache) > 256:
                self.rpc_cache.popitem(last=False)
            self.endpoint.send(response)

    async def _record_job(self, request):
        self.record_job_status["state"] = "RUNNING"
        try:
            await asyncio.to_thread(self._operation, request["operation"], request.get("data", {}))
            self.record_job_status["state"] = "COMPLETE"
        except Exception as exc:
            self.record_job_status.update(state="FAILED", error=str(exc)[:1000])

    def _operation(self, operation, data):
        if operation == "home":
            with self.lock:
                return self._start_homing(data)
        if operation == "home_cancel":
            with self.lock:
                if data.get("epoch") != self.control.epoch:
                    raise Rejected("STALE_EPOCH")
                if self.control.mode != Mode.HOMING:
                    raise Rejected("HOMING_NOT_RUNNING")
                self._end_homing("HOMING_CANCELLED")
                self.control._transition(
                    Mode.HOLD if self.homing_status["state"] == "FAILED" else Mode.DISARMED,
                    "HOMING_CANCELLED",
                )
                return self.control.status()
        if operation in ("arm", "start", "toggle", "pause", "estop", "disarm", "reset"):
            with self.lock:
                if operation != "estop" and data.get("epoch") != self.control.epoch:
                    raise Rejected("STALE_EPOCH")
                if self.control.mode == Mode.HOMING:
                    if operation not in ("pause", "estop", "disarm"):
                        raise Rejected("HOMING_BUSY")
                    self._end_homing("HOMING_CANCELLED")
                    final_mode = (
                        Mode.ESTOP
                        if operation == "estop"
                        else Mode.HOLD
                        if self.homing_status["state"] == "FAILED"
                        else Mode.DISARMED
                    )
                    self.control._transition(final_mode, "HOMING_CANCELLED")
                    return self.control.status()
                if (
                    (operation in ("reset", "arm")
                     or operation == "start" and self.control.operator_paused)
                    and self.cfg.safety.checks.stop_confirmation
                    and not self.driver.stop_confirmed()
                ):
                    raise Rejected("DRIVER_STOP_NOT_CONFIRMED")
                q, stamp = self.driver.read()
                self.control.request(operation, q, self.health(stamp))
                if operation == "reset":
                    self.supervisor_fault.clear()
                return self.control.status()
        if operation == "record_name":
            return (self.recorder.name_status(data["name"]) if "name" in data
                    else self.recorder.automatic_name_status())
        if operation == "task_list":
            return self.recorder.task_sessions.snapshot(catalog=True)
        if operation in ("task_create", "task_select"):
            with self.lock, self.recorder.lock:
                if (self.control.mode != Mode.DISARMED or self.recorder.state != "IDLE"
                        or self.record_job and not self.record_job.done()):
                    raise Rejected("TASK_CHANGE_REQUIRES_IDLE")
                return (self.recorder.task_sessions.create(data.get("prompt"))
                        if operation == "task_create"
                        else self.recorder.task_sessions.select(data.get("task_id")))
        if operation == "record_start":
            if self.cfg.safety.attended_test.enabled and not self.cfg.safety.attended_test.allow_recording:
                raise Rejected("ATTENDED_TEST_RECORDING_DISABLED")
            with self.lock:
                if self.control.mode not in (Mode.DISARMED, Mode.READY, Mode.HOLD):
                    raise Rejected("PAUSE_REQUIRED")
            frames = self.frames.snapshot()
            if not self.frames.healthy(self.cfg):
                raise Rejected("CAMERA_NOT_READY")
            self.recorder.start(data.get("name"), {n: im.shape for n, (im, _) in frames.items()})
            with self.lock:
                self.control.recording = True
            return self.recorder.status()
        if operation == "record_finish":
            with self.lock:
                self.control.hold("EPISODE_FINISHED")
                self.control.recording = False
            self.driver.stop()
            return self.recorder.finish(data["outcome"])
        if operation == "config_get":
            return self.store.read()
        if operation == "config_defaults":
            return self.store.read_defaults()
        if operation in ("config_save", "config_apply"):
            with self.lock:
                if (
                    self.control.mode != Mode.DISARMED
                    or self.recorder.state != "IDLE"
                    or self.record_job
                    and not self.record_job.done()
                ):
                    raise Rejected("DISARM_REQUIRED")
                if operation == "config_save":
                    return self.store.save(data["config"], data["revision"])
                previous_contract = self.cfg.control_contract
                result = self.store.apply_safety(data["revision"])
                io = getattr(self.driver, "io", None)
                if io is not None:
                    with io.lock:
                        io.history_seconds = self.cfg.safety.stationary_window_seconds + 0.1
                if previous_contract != self.cfg.control_contract:
                    self.control.audit.append(
                        {
                            "event": "safety_hot_update",
                            "previous_contract": previous_contract,
                            "active_contract": self.cfg.control_contract,
                            "monotonic": time.monotonic(),
                        }
                    )
                return result
        if operation == "datasets_list":
            return self.inspector.list()
        if operation == "datasets_inspect":
            return self.inspector.inspect(data["id"], data.get("deep", False))
        if operation == "datasets_preview":
            return {
                "jpeg": base64.b64encode(
                    self.inspector.preview(data["id"], data["camera"], data.get("frame", 0))
                ).decode()
            }
        raise ValueError("UNKNOWN_OPERATION")

    def _start_homing(self, data):
        if data.get("epoch") != self.control.epoch:
            raise Rejected("STALE_EPOCH")
        role = data.get("target", "robot")
        if role not in ("robot", "leader"):
            raise Rejected("HOMING_INVALID_TARGET")
        if self.control.mode != Mode.DISARMED:
            raise Rejected("DISARM_REQUIRED")
        if self.recorder.state != "IDLE" or self.record_job and not self.record_job.done():
            raise Rejected("RECORDER_BUSY")
        if self.store.read()["restart_required"]:
            raise Rejected("HOMING_RESTART_REQUIRED")
        if getattr(self.cfg, role).backend == "sii_serial":
            raise Rejected("HOMING_ROS_REQUIRED")
        if (
            not self.cfg.general.simulation
            and self.cfg.safety.checks.hardware_validation
            and not self.cfg.safety.hardware_validated
            and not self.control.test_available()
        ):
            raise Rejected("HARDWARE_COMMISSIONING_REQUIRED")
        trial = self.cfg.safety.attended_test
        if trial.enabled and not self.control.test_available():
            raise Rejected("ATTENDED_TEST_UNAVAILABLE")
        if (
            trial.enabled
            and trial.expires_at is not None
            and trial.max_start_window_seconds is not None
            and trial.expires_at - self.control.wall_clock() > trial.max_start_window_seconds
        ):
            raise Rejected("ATTENDED_TEST_UNAVAILABLE")
        # Require an attended UI lease even if the leader itself has no feedback;
        # follower homing does not depend on a working teleoperator arm.
        if (
            not self.home_operator_ok
            or time.monotonic() - self.home_operator_at > self.cfg.safety.leader_lease_ms / 1000
        ):
            raise Rejected("HOMING_OPERATOR_REQUIRED")
        if role == "robot":
            if self.cfg.safety.checks.stop_confirmation and not self.driver.stop_confirmed():
                raise Rejected("DRIVER_STOP_NOT_CONFIRMED")
            candidate = HomingSession(self.cfg, self.driver, role, self._home_guard)
            # Preflight checks position/limits without requiring HOMING authority yet.
            candidate.guard = lambda: None
            candidate.preflight()
            candidate.guard = self._home_guard
        else:
            candidate = None
        self.control._transition(Mode.HOMING, "HOMING_REQUEST")
        self.homing = candidate
        self.leader_home_report = None
        self.homing_status = {
            "id": self.control.epoch,
            "target": role,
            "state": "QUEUED",
            "progress": 0.0,
            "error": None,
        }
        if trial.enabled:
            self.control.test_started = self.control.clock()
            self.control.test_used = True
        return dict(self.homing_status)

    def _home_guard(self):
        if self.control.mode != Mode.HOMING or self.supervisor_fault.is_set():
            raise Rejected("HOMING_CANCELLED")
        if (
            not self.home_operator_ok
            or time.monotonic() - self.home_operator_at > self.cfg.safety.leader_lease_ms / 1000
        ):
            raise Rejected("HOMING_OPERATOR_REQUIRED")
        if self.cfg.safety.attended_test.enabled and self.control._test_expired():
            raise Rejected("ATTENDED_TEST_EXPIRED")

    def _end_homing(self, reason, failed=False, complete=False):
        if not self.homing_status or self.homing_status["state"] in ("COMPLETE", "FAILED", "CANCELLED"):
            return
        if self.homing:
            try:
                if not complete:
                    self.homing.cancel(reason, failed=failed)
                    if self.homing.state == "FAILED":
                        failed, reason = True, self.homing.last_error
            finally:
                self.homing.close()
                self.homing = None
        self.homing_status.update(
            state="COMPLETE" if complete else "FAILED" if failed else "CANCELLED",
            error=None if complete else reason,
        )
        if complete:
            self.homing_status["progress"] = 1.0
        self.control.audit.append(
            {"event": "homing_finished", **self.homing_status, "monotonic": time.monotonic()}
        )
        try:
            self.driver.stop()
        except Exception:
            logger.exception("Homing cleanup could not stop the driver")
            self.homing_status.update(state="FAILED", error="HOMING_EXIT_FAILED")

    def _advance_homing(self):
        try:
            self._home_guard()
            if self.homing_status["target"] == "robot":
                self.homing.tick()
                self.homing_status.update(self.homing.status())
                complete = self.homing.state == "COMPLETE"
            else:
                self.driver.stop()
                report = self.leader_home_report
                if report is None:
                    if time.monotonic() - self.control.pending_since > 5:
                        raise Rejected("HOMING_LEADER_TIMEOUT")
                    return
                if time.monotonic() - report["received_at"] > self.cfg.safety.leader_lease_ms / 1000:
                    raise Rejected("HOMING_LEADER_TIMEOUT")
                self.homing_status.update(
                    {k: report[k] for k in ("state", "progress", "method", "phase", "error",
                                          "elapsed_ms", "joint_residual", "gripper_required") if k in report}
                )
                if report["state"] in ("FAILED", "CANCELLED"):
                    raise Rejected(report.get("error") or "HOMING_FAILED")
                complete = report["state"] == "COMPLETE"
            if complete:
                # Marking the session terminal must not prevent its cleanup.
                self.homing_status["state"] = "RUNNING"
                self._end_homing("HOMING_COMPLETE", complete=True)
                self.control._transition(
                    Mode.HOLD if self.homing_status["state"] == "FAILED" else Mode.DISARMED,
                    "HOMING_COMPLETE" if self.homing_status["state"] == "COMPLETE" else "HOMING_EXIT_FAILED",
                )
        except Exception as exc:
            if self.homing_status:
                self.homing_status["state"] = "RUNNING"
            self._end_homing(str(exc), failed=True)
            self.control.hold("HOMING_FAILED")

    def _control_loop(self):
        next_record = time.monotonic()
        enabling_since = None
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                q, stamp = self.driver.read()
                with self.lock:
                    if self.supervisor_fault.is_set():
                        self.control.hold("CONTROL_LOOP_STALLED")
                    self.control.recording = self.recorder.state == "RECORDING"
                    if self.control.mode == Mode.HOMING:
                        if time.monotonic() - stamp > self.cfg.safety.state_max_age_ms / 1000:
                            raise Rejected("HOMING_STALE_STATE")
                        self._advance_homing()
                        self.applied = None
                    else:
                        if self.homing_status and self.homing_status["state"] in ("QUEUED", "RUNNING"):
                            self._end_homing("HOMING_CANCELLED", failed=True)
                        result = self.control.tick(q, self.health(stamp))
                        mode = self.control.mode
                        if mode in (Mode.HUMAN, Mode.POLICY):
                            self.driver.enable()
                            if (
                                self.cfg.safety.checks.enable_confirmation
                                and not self.driver.enabled_confirmed()
                            ):
                                enabling_since = enabling_since or started
                                if started - enabling_since > self.cfg.safety.driver_enable_timeout_seconds:
                                    self.control.hold("DRIVER_ENABLE_TIMEOUT")
                                    self.driver.stop()
                            elif result:
                                enabling_since = None
                                self.driver.command(result.action)
                                self.applied, self.applied_epoch = result, result.epoch
                            else:
                                # No new command was applied during inference wait.
                                self.applied = None
                        else:
                            self.driver.stop()
                            enabling_since = None
                            self.applied = None
                        if (
                            started >= next_record
                            and self.applied
                            and self.applied_epoch == self.control.epoch
                        ):
                            next_record = max(next_record + 1 / self.cfg.recording.fps, started + 0.0001)
                            if self.recorder.submit(
                                q, self.applied, self.frames.snapshot(), self.current_observation_id
                            ):
                                self.control.release_frame = False
                self.last_loop = time.monotonic()
            except Exception as exc:
                with self.lock:
                    self.control.hold("DRIVER_ERROR_" + type(exc).__name__)
                    self._end_homing(str(exc), failed=True)
                try:
                    self.driver.stop()
                except Exception:
                    logger.exception("Driver stop failed; hardware watchdog must stop the robot")
                self.last_loop = time.monotonic()
            self.stop_event.wait(max(0, 1 / self.cfg.safety.control_hz - (time.monotonic() - started)))

    def _supervise(self):
        # Independent of asyncio, model requests, image encoding and disk writes.
        # Hardware watchdog remains mandatory for a stalled interpreter/process.
        while not self.stop_event.wait(self.cfg.safety.watchdog_check_ms / 1000):
            if not self.cfg.safety.checks.control_loop_watchdog:
                continue
            if time.monotonic() - self.last_loop > self.cfg.safety.watchdog_ms / 1000:
                if self.control.mode not in (Mode.DISARMED, Mode.HOLD, Mode.ESTOP):
                    self.supervisor_fault.set()
                try:
                    self.driver.stop()
                except Exception:
                    logger.exception("Supervisor stop failed; hardware watchdog must stop the robot")
                # Avoid waiting on a blocked control-loop lock before requesting hardware stop.
                if self.lock.acquire(blocking=False):
                    try:
                        self.control.hold("CONTROL_LOOP_STALLED")
                    finally:
                        self.lock.release()

    async def _publish(self):
        next_observation = 0.0
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                q, stamp = self.driver.read()
                with self.lock:
                    obs_id = self.control.observe()
                    observed_at = self.control.observations[obs_id]
                    self.current_observation_id = obs_id
                    status = self.control.status()
                    homing_status = dict(self.homing_status) if self.homing_status else None
                    audit, self.control.audit = self.control.audit, []
                if audit:
                    await asyncio.to_thread(self._audit, audit)
                message = {
                    "type": "status",
                    **status,
                    "q": q,
                    "observation_id": obs_id,
                    "healthy": self.health(stamp),
                    "state_age_ms": (time.monotonic() - stamp) * 1000,
                    "cameras": {
                        n: {"age_ms": (time.monotonic() - s) * 1000}
                        for n, (_, s) in self.frames.snapshot().items()
                    },
                    "recorder": self.recorder.status(),
                    "task_session": self.recorder.task_sessions.snapshot(),
                    "rejected_commands": self.rejected,
                    "record_job": self.record_job_status,
                    "recording_required_for_hil": self.cfg.recording.require_recording_for_hil,
                    "homing": homing_status,
                    "last_rejection": self.last_rejection,
                    "simulation": self.cfg.general.simulation,
                    "stop_confirmed": self.driver.stop_confirmed(),
                    "stop_confirmation": self.cfg.robot.stop_strategy,
                    "motion_permitted": (
                        self.control.test_available()
                        if self.cfg.safety.attended_test.enabled
                        else self.cfg.general.simulation
                        or self.cfg.safety.hardware_validated
                        or not self.cfg.safety.checks.hardware_validation
                    ),
                    "attended_test": self.cfg.safety.attended_test.enabled,
                    "attended_limits": self.cfg.safety.attended_test.model_dump(),
                    "recording_permitted": not self.cfg.safety.attended_test.enabled
                    or self.cfg.safety.attended_test.allow_recording,
                }
                self.endpoint.send(message)
                if time.monotonic() >= next_observation:
                    next_observation = time.monotonic() + 1 / self.cfg.network.observation_hz
                    images = await asyncio.to_thread(self.frames.observation_images)
                    observation = {
                        "type": "observation",
                        "status": status,
                        "q": q,
                        "observation_id": obs_id,
                        "images": images,
                        "task": self.recorder.task_sessions.snapshot()["active"]["prompt"],
                    }
                    if self.runner:
                        self.runner.update(observation, observed_at=observed_at)
                    self.endpoint.send(observation)
            except Exception as exc:
                logger.debug("Telemetry unavailable: %s", type(exc).__name__)
            await asyncio.sleep(max(0, 1 / self.cfg.network.telemetry_hz - (time.monotonic() - started)))

    def _audit(self, events):
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a") as output:
                for event in events:
                    output.write(
                        json.dumps({"robot_id": self.cfg.general.robot_id, "wall_time": time.time(), **event})
                        + "\n"
                    )
        except OSError:
            with self.lock:
                self.control.hold("AUDIT_WRITE_FAILED")
            self.driver.stop()
            raise

    async def close(self):
        self.stop_event.set()
        with self.lock:
            self._end_homing("SHUTDOWN", failed=True)
        if self.driver:
            self.driver.stop()
        await self.endpoint.close()
        for task in [self.network_task, self.video_task, *self.tasks]:
            if task:
                task.cancel()
        await asyncio.gather(
            *[t for t in [self.network_task, self.video_task, *self.tasks] if t], return_exceptions=True
        )
        await self.video.close()
        if self.runner:
            self.runner.close()
        if self.record_job:
            await self.record_job
        self.recorder.close()
        if self.cameras:
            self.cameras.close()
        for thread in (getattr(self, "thread", None), getattr(self, "watchdog", None)):
            if thread:
                thread.join(timeout=2)
        with self.lock:
            audit, self.control.audit = self.control.audit, []
        if audit:
            self._audit(audit)
        if self.driver:
            self.driver.close()


class ClientRuntime:
    def __init__(self, store):
        self.store, self.cfg = store, store.current
        self.endpoint = Endpoint(self.cfg, "client", self.on_message, self.disconnected)
        self.frames = Frames()
        self.video = VideoPeer(self.cfg, "client", self.frames, self.endpoint.send)
        self.status = {"mode": "OFFLINE"}
        self.status_at = 0.0
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.leader = None
        self.runner = None
        self.prepared_epoch = None
        self.position_epoch = None
        self.last_epoch = None
        self.seq = 0
        self.error = None
        self.tasks = set()
        self.offset = [0.0] * 14
        self.operator_seen_at = 0.0
        self.leader_q, self.leader_stamp = None, 0.0
        self.homing = None
        self.homing_report = None
        self.leader_transition = None
        self.gripper_takeover = None
        self.gripper_takeover_status = None

    async def start(self):
        self.leader = leader_adapter(self.cfg)
        if self.cfg.policy.runner_location == "client":
            self.runner = PolicyRunner(self.cfg, "client", self.endpoint.send)
            self.runner.start()
        self.thread = threading.Thread(target=self._leader_loop, daemon=True, name="leader-control")
        self.thread.start()
        await self.endpoint.start()

    def disconnected(self):
        with self.lock:
            self.status = {"mode": "OFFLINE"}
            self.prepared_epoch = None
            self.position_epoch = None
        self._cancel_local_homing("TRANSPORT_LOST")

    async def on_message(self, msg):
        if msg["type"] == "status":
            with self.lock:
                self.status, self.status_at = msg, time.monotonic()
        elif msg["type"] == "observation":
            if self.runner:
                self.runner.update(msg)
            if not self.cfg.network.webrtc_enabled:
                import cv2
                import numpy as np

                for name, encoded in msg["images"].items():
                    frame = cv2.imdecode(
                        np.frombuffer(base64.b64decode(encoded), dtype=np.uint8), cv2.IMREAD_COLOR
                    )
                    if frame is not None:
                        self.frames.put(name, frame)
        elif msg["type"] == "peer" and not msg.get("connected"):
            self.disconnected()
        elif msg["type"] == "signal":
            task = asyncio.create_task(self.video.receive(msg))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def _local_home_guard(self):
        now = time.monotonic()
        home = self.status.get("homing") or {}
        if (
            self.stop_event.is_set()
            or self.status.get("mode") != "HOMING"
            or home.get("id") != (self.homing_report or {}).get("id")
            or now - self.status_at > self.cfg.safety.leader_lease_ms / 1000
            or now - self.operator_seen_at > self.cfg.safety.operator_lease_seconds
        ):
            raise Rejected("HOMING_CANCELLED")

    def _cancel_local_homing(self, reason):
        if self.homing is not None:
            try:
                self.homing.cancel(reason, failed=True)
                self.homing_report.update(self.homing.status())
            finally:
                self.homing.close()
                self.homing = None

    def _leader_homing(self, status):
        home = status.get("homing") or {}
        if status.get("mode") != "HOMING" or home.get("target") != "leader":
            self._cancel_local_homing("HOMING_CANCELLED")
            return False
        if self.homing_report is None or self.homing_report.get("id") != home["id"]:
            self._cancel_local_homing("HOMING_CANCELLED")
            self.homing_report = {"id": home["id"], "state": "QUEUED", "error": None}
            if self.store.read()["restart_required"]:
                raise Rejected("HOMING_RESTART_REQUIRED")
            self.homing = HomingSession(self.cfg, self.leader, "leader", self._local_home_guard)
        if self.homing is not None:
            self.homing.tick()
            self.homing_report.update(self.homing.status())
            if self.homing.state == "COMPLETE":
                self.homing.close()
                self.homing = None
        return True

    def _prepare_position_mode(self, epoch):
        """Confirm hardware mode for each transition; ROS drivers can restart independently."""
        if getattr(self, "position_epoch", None) != epoch:
            self.leader.set_manual(False)
            self.position_epoch = epoch

    def _send_leader_heartbeat(self, healthy):
        self.endpoint.send({
            "type": "leader_heartbeat", "healthy": healthy,
            "operator_healthy": time.monotonic() - self.operator_seen_at
            <= self.cfg.safety.operator_lease_seconds,
            "homing": dict(self.homing_report) if self.homing_report else None,
            "transition": getattr(self, "leader_transition", None),
        })

    def _transition_progress(self, status, stage, pose, error=None):
        self.leader_transition = {"epoch": status.get("epoch"), "stage": stage,
                                  "pose": list(pose), "error_code": error}
        # Publish before potentially blocking driver-mode acknowledgement.
        self._send_leader_heartbeat(error is None)

    def _feedback_step(self, previous, target):
        """Bound commands derived from measured follower feedback."""
        previous, target = vector(previous), vector(target)
        low, high = self.cfg.safety.joint_min * 2, self.cfg.safety.joint_max * 2
        if self.cfg.safety.checks.position_limits:
            # Closed grippers may report a small negative measurement offset.
            # Preserve raw observations; use legal gripper widths for commands.
            for values in (previous, target):
                for index in (6, 13):
                    values[index] = max(low[index], min(high[index], values[index]))
            if any(v < lo or v > hi for values in (previous, target)
                   for v, lo, hi in zip(values, low, high)):
                raise Rejected("LEADER_POSITION_LIMIT")
        maximum = [
            v / self.cfg.leader.feedback_hz if self.cfg.safety.checks.velocity_limits else float("inf")
            for v in self.cfg.safety.velocity_max * 2
        ]
        return [p + max(-d, min(d, t - p)) for p, t, d in zip(previous, target, maximum)]

    def _leader_loop(self):
        previous_feedback = None
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                with self.lock:
                    status, age = dict(self.status), started - self.status_at
                if status.get("mode") != "HOMING":
                    self._cancel_local_homing("HOMING_CANCELLED")
                q, stamp = self.leader.read()
                with self.lock:
                    self.leader_q, self.leader_stamp = q.copy(), stamp
                    status, age = dict(self.status), started - self.status_at
                fresh = (
                    not self.cfg.safety.checks.state_freshness
                    or started - stamp <= self.cfg.safety.state_max_age_ms / 1000
                )
                fresh = fresh and (
                    not self.cfg.safety.checks.operator_heartbeat
                    or started - self.operator_seen_at <= self.cfg.safety.operator_lease_seconds
                )
                mode = status.get("mode")
                epoch = status.get("epoch")
                remote_fresh = (
                    not self.cfg.safety.checks.leader_heartbeat
                    or age <= self.cfg.safety.leader_lease_ms / 1000
                )
                self._send_leader_heartbeat(fresh and (
                    mode in ("OFFLINE", "DISARMED", "READY", "SAFE_HOLD", "ESTOP_LATCHED")
                    or remote_fresh
                ))
                if not fresh or not remote_fresh:
                    raise Rejected("LEADER_OR_FEEDBACK_STALE")
                if self._leader_homing(status):
                    self.error = None
                    self.stop_event.wait(
                        max(0, 1 / self.cfg.leader.feedback_hz - (time.monotonic() - started))
                    )
                    continue
                target = vector(status["q"])
                trial = self.cfg.safety.attended_test
                if trial.enabled and trial.gripper_commands is not None:
                    target[6], target[13] = trial.gripper_commands
                elif self.cfg.safety.checks.position_limits:
                    for index in (6, 13):
                        target[index] = max(self.cfg.safety.joint_min[6],
                                            min(self.cfg.safety.joint_max[6], target[index]))
                if epoch != self.last_epoch:
                    self.last_epoch, self.seq = epoch, 0
                    self.position_epoch = None
                    self.leader_transition = None
                    self.gripper_takeover = None
                    self.gripper_takeover_status = None
                    previous_feedback = q
                if mode in ("TAKEOVER_PENDING", "RESUME_PENDING") and epoch != self.prepared_epoch:
                    if (
                        not trial.enabled
                        and self.cfg.safety.checks.alignment
                        # Leave room for feedback noise and transport delay before
                        # the robot independently checks the full safety tolerance.
                        and alignment_report(q, target, self.cfg.safety.alignment_tolerance / 2)["error_rad"]
                        > self.cfg.safety.alignment_tolerance / 2
                    ):
                        # The follower remains stopped while authority is pending.
                        # Preload present leader position before changing control
                        # mode, then approach measured follower state at bounded speed.
                        output = self._feedback_step(q if self.leader.manual else previous_feedback or q, target)
                        if self.position_epoch != epoch:
                            self._transition_progress(status, "position_mode", q)
                        self._prepare_position_mode(epoch)
                        self._transition_progress(status, "aligning", q)
                        self.leader.feedback(output)
                        previous_feedback = output
                    else:
                        manual = mode == "TAKEOVER_PENDING"
                        if manual:
                            self._transition_progress(status, "manual_mode", q)
                            self.leader.set_manual(True)
                        else:
                            self._transition_progress(status, "position_mode", q)
                            self._prepare_position_mode(epoch)
                        q, _ = self.leader.read()
                        # Preserve the first manual command at the measured
                        # follower pose, including passive gripper coordinates.
                        self.offset = [t - a for t, a in zip(target, q)] if manual else [0.0] * 14
                        # Passive grippers use absolute openings after independent
                        # soft pickup, never the rotational joint's relative offset.
                        self.offset[6] = self.offset[13] = 0.0
                        if manual and not trial.enabled:
                            self.gripper_takeover = GripperTakeover(q, target,
                                self.cfg.safety.joint_min[6], self.cfg.safety.joint_max[6])
                            self.gripper_takeover_status = self.gripper_takeover.status()
                        if trial.enabled and trial.gripper_commands is None:
                            # Free grippers follow the leader's absolute command units.
                            self.offset[6] = self.offset[13] = 0.0
                        self.prepared_epoch = epoch
                        self._transition_progress(status, "ready", q)
                        ready_pose = target if trial.enabled else [a + b for a, b in zip(q, self.offset)]
                        if self.gripper_takeover is not None:
                            ready_pose[6], ready_pose[13] = self.gripper_takeover.apply(q)
                        self.endpoint.send(
                            {
                                "type": "mode_ready",
                                "epoch": epoch,
                                "pose": ready_pose,
                            }
                        )
                elif mode == "HUMAN_ACTIVE":
                    self.seq += 1
                    action = [a + b for a, b in zip(q, self.offset)]
                    if self.gripper_takeover is not None:
                        action[6], action[13] = self.gripper_takeover.apply(q)
                        self.gripper_takeover_status = self.gripper_takeover.status()
                    if trial.enabled and trial.gripper_commands is not None:
                        action[6], action[13] = trial.gripper_commands
                    self.endpoint.send(
                        {
                            "type": "human_action",
                            "epoch": epoch,
                            "seq": self.seq,
                            "observation_id": status["observation_id"],
                            "action": action,
                        }
                    )
                elif mode in ("POLICY_ACTIVE", "RESUME_PENDING") and not self.leader.manual:
                    # Rate-limit measured follower feedback locally. Never mirror model targets.
                    output = self._feedback_step(previous_feedback or q, target)
                    self.leader.feedback(output)
                    previous_feedback = output
                self.error = None
            except Exception as exc:
                self.error = str(exc)[:160]
                if (status.get("mode") in ("TAKEOVER_PENDING", "RESUME_PENDING")
                        and getattr(self, "leader_q", None) is not None):
                    # Keep the failed stage as well as its driver error code.
                    stage = (getattr(self, "leader_transition", None) or {}).get("stage", "error")
                    self._transition_progress(status, stage, self.leader_q, self.error)
                self._cancel_local_homing(self.error)
                home = self.status.get("homing") or {}
                if self.status.get("mode") == "HOMING" and home.get("target") == "leader":
                    self.homing_report = {"id": home["id"], "state": "FAILED", "error": self.error}
                self._send_leader_heartbeat(False)
            self.stop_event.wait(max(0, 1 / self.cfg.leader.feedback_hz - (time.monotonic() - started)))

    def snapshot(self):
        with self.lock:
            result = dict(self.status)
            result["leader_q"] = self.leader_q
            result["gripper_takeover"] = (
                getattr(self, "gripper_takeover_status", None)
                if self.status.get("mode") == "HUMAN_ACTIVE"
                and self.status.get("epoch") == self.prepared_epoch else None
            )
            result["leader_state_age_ms"] = (
                (time.monotonic() - self.leader_stamp) * 1000 if self.leader_stamp else None
            )
        result.update(
            connected=self.endpoint.online,
            telemetry_age_ms=(time.monotonic() - self.status_at) * 1000 if self.status_at else None,
            leader_error=self.error,
            leader_homing_supported=self.cfg.leader.backend != "sii_serial",
            homing_state_max_age_ms=self.cfg.safety.state_max_age_ms,
            language=self.cfg.ui.language,
            shortcuts={
                k: getattr(self.cfg.ui, k)
                for k in ("intervention_key", "success_key", "failure_key", "discard_key")
            },
            video={
                n: {"age_ms": (time.monotonic() - s) * 1000} for n, (_, s) in self.frames.snapshot().items()
            },
        )
        return result

    async def close(self):
        self.stop_event.set()
        self._cancel_local_homing("SHUTDOWN")
        self.endpoint.send({"type": "stop"})
        await asyncio.sleep(0.05)
        await self.endpoint.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.video.close()
        if self.runner:
            self.runner.close()
        if getattr(self, "thread", None):
            self.thread.join(timeout=3)
        if self.leader:
            self.leader.close()

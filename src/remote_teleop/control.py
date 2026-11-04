"""Robot-owned control authority. No I/O, wall-clock assumptions or hardware imports."""

from __future__ import annotations

import math
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum

from .config import AppConfig


class Rejected(ValueError):
    pass


class Mode(str, Enum):
    DISARMED = "DISARMED"
    READY = "READY"
    HOMING = "HOMING"
    POLICY = "POLICY_ACTIVE"
    TAKEOVER = "TAKEOVER_PENDING"
    HUMAN = "HUMAN_ACTIVE"
    RESUME = "RESUME_PENDING"
    HOLD = "SAFE_HOLD"
    ESTOP = "ESTOP_LATCHED"


def vector(value, size=14) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise Rejected("INVALID_DIMENSION")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise Rejected("INVALID_NUMBER")
    return [float(v) for v in value]


def alignment_report(leader, target, tolerance):
    """Angular alignment uses six joints per arm; grippers are metre coordinates."""
    leader, target = vector(leader), vector(target)
    joint = max((i for i in range(14) if i % 7 != 6),
                key=lambda i: abs(leader[i] - target[i]))
    error = abs(leader[joint] - target[joint])
    return {
        "joint": f"{'left' if joint < 7 else 'right'}.joint_{joint % 7 + 1}",
        "leader_rad": leader[joint], "target_rad": target[joint],
        "error_rad": error, "error_degrees": math.degrees(error),
        "tolerance_rad": tolerance, "tolerance_degrees": math.degrees(tolerance),
        "grippers": [{"arm": arm, "leader_m": leader[i], "target_m": target[i],
                      "difference_mm": abs(leader[i] - target[i]) * 1000}
                     for arm, i in (("left", 6), ("right", 13))],
    }


@dataclass(frozen=True)
class AppliedAction:
    action: list[float]
    policy_action: list[float]
    intervention: float
    hil_state: float
    source: str
    epoch: str
    observation_id: str
    command_seq: int
    requested_action: list[float]


class Controller:
    """Called under the robot runtime lock. Epoch changes invalidate all old work."""

    def __init__(self, cfg: AppConfig, clock=time.monotonic, wall_clock=time.time):
        self.cfg, self.clock, self.wall_clock = cfg, clock, wall_clock
        self.mode = Mode.DISARMED
        self.reason = "DISARMED"
        self.epoch = uuid.uuid4().hex
        self.observations: OrderedDict[str, float] = OrderedDict()
        self.last_seq = -1
        self.last_command = self.clock()
        self.inference_wait = None
        self.inference_progress_seq = None
        self.policy_error = None
        self.last_leader = 0.0
        self.leader_ok = False
        self.recording = False
        self.pending_since = self.clock()
        self.pending = None
        self.last_target = None
        self.velocity = [0.0] * 14
        self.last_tick = self.clock()
        self.release_frame = False
        self.resuming_from_human = False
        self.leader_ready = False
        self.audit: list[dict] = []
        self.test_reference = None
        self.test_started = None
        self.test_used = False
        self.limit_detail = None
        self.tracking_since = None
        self.tracking_error_detail = None
        self.transition_detail = None
        self.leader_progress_at = None

    def test_available(self):
        trial = self.cfg.safety.attended_test
        return (
            trial.enabled
            and (not trial.single_use or not self.test_used)
            and (trial.expires_at is None or self.wall_clock() < trial.expires_at)
        )

    def _control_pose(self, measured):
        q = vector(measured)
        if (
            self.cfg.safety.attended_test.enabled
            and self.cfg.safety.attended_test.gripper_commands is not None
        ):
            q[6], q[13] = self.cfg.safety.attended_test.gripper_commands
        elif self.cfg.safety.checks.position_limits:
            # A closed gripper can report a small negative offset. Start its
            # command trajectory at the nearest valid command, not outside it.
            for i in (6, 13):
                q[i] = max(self.cfg.safety.joint_min[6], min(self.cfg.safety.joint_max[6], q[i]))
        return q

    def _test_expired(self):
        trial = self.cfg.safety.attended_test
        return (trial.expires_at is not None and self.wall_clock() >= trial.expires_at) or (
            trial.duration_seconds is not None
            and self.test_started is not None
            and self.clock() - self.test_started >= trial.duration_seconds
        )

    def _test_distance(self, values):
        return max(abs(values[i] - self.test_reference[i]) for i in range(14) if i % 7 != 6)

    def _transition(self, mode: Mode, reason: str):
        old = self.mode
        # Freeze the last pending-stage evidence before rotating the epoch.
        # Late heartbeats must never overwrite a latched incident.
        detail = self.transition_status() if old in (Mode.TAKEOVER, Mode.RESUME) else None
        self.transition_detail = detail if mode == Mode.HOLD else None
        self.leader_progress_at = None
        self.mode, self.reason = mode, reason
        if reason != "POSITION_LIMIT":
            self.limit_detail = None
        self.tracking_since = None
        if reason != "TRACKING_ERROR":
            self.tracking_error_detail = None
        self.epoch = uuid.uuid4().hex
        self.last_seq, self.pending = -1, None
        self.inference_wait = None
        self.inference_progress_seq = None
        self.policy_error = None
        self.velocity = [0.0] * 14
        self.pending_since = self.clock()
        self.last_command = self.clock()
        self.leader_ready = False
        self.release_frame = False
        self.resuming_from_human = False
        if mode in (Mode.TAKEOVER, Mode.RESUME):
            self.transition_detail = {"epoch": self.epoch, "transition": mode.value,
                                      "stage": "awaiting_leader"}
        self.audit.append(
            {
                "event": "transition",
                "from": old.value,
                "to": mode.value,
                "reason": reason,
                "epoch": self.epoch,
                "monotonic": self.clock(),
                **({"transition_detail": self.transition_detail}
                   if self.transition_detail is not None and mode == Mode.HOLD else {}),
            }
        )
        self.audit = self.audit[-1000:]

    def observe(self) -> str:
        identifier = uuid.uuid4().hex
        self.observations[identifier] = self.clock()
        while len(self.observations) > 600:
            self.observations.popitem(last=False)
        return identifier

    def heartbeat(self, healthy: bool, progress=None):
        self.last_leader = self.clock()
        self.leader_ok = healthy
        if progress is not None:
            self.leader_progress(progress)

    def leader_progress(self, progress):
        if (not isinstance(progress, dict) or progress.get("epoch") != self.epoch
                or self.mode not in (Mode.TAKEOVER, Mode.RESUME) or self.leader_ready):
            return False
        stage = progress.get("stage")
        if stage not in ("aligning", "position_mode", "manual_mode", "ready", "error"):
            return False
        try:
            report = alignment_report(progress.get("pose"), self.last_target,
                                      self.cfg.safety.alignment_tolerance / 2)
        except (ValueError, TypeError):
            return False
        error = progress.get("error_code")
        if error is not None and (not isinstance(error, str) or len(error) > 64
                                  or not error.isascii() or not error.isidentifier() or not error.isupper()):
            error = "LEADER_PREPARE_FAILED"
        self.transition_detail = {"epoch": self.epoch, "transition": self.mode.value,
                                  "stage": stage, **report, "error_code": error,
                                  "alignment_enabled": self.cfg.safety.checks.alignment}
        self.leader_progress_at = self.clock()
        return True

    def transition_status(self):
        if self.transition_detail is None:
            return None
        detail = dict(self.transition_detail)
        if self.mode in (Mode.TAKEOVER, Mode.RESUME):
            detail["elapsed_ms"] = max(0, (self.clock() - self.pending_since) * 1000)
            detail["progress_age_ms"] = (max(0, (self.clock() - self.leader_progress_at) * 1000)
                                         if self.leader_progress_at is not None else None)
        return detail

    def request(self, operation: str, measured: list[float], healthy: bool):
        q = self._control_pose(measured)
        if operation == "estop":
            if self.mode != Mode.ESTOP:
                self._transition(Mode.ESTOP, "OPERATOR_ESTOP")
            return
        if operation == "disarm":
            if self.mode == Mode.ESTOP:
                raise Rejected("RESET_REQUIRED")
            self._transition(Mode.DISARMED, "DISARMED")
            return
        if operation == "reset":
            if self.mode not in (Mode.HOLD, Mode.ESTOP):
                raise Rejected("INVALID_TRANSITION")
            if not healthy:
                raise Rejected("HEALTH_CHECK_FAILED")
            self._transition(Mode.DISARMED, "RESET_COMPLETE")
            return
        if self.mode == Mode.HOMING:
            raise Rejected("HOMING_BUSY")
        if (
            not healthy
            or self.cfg.safety.checks.leader_heartbeat
            and (
                not self.leader_ok or self.clock() - self.last_leader > self.cfg.safety.leader_lease_ms / 1000
            )
        ):
            raise Rejected("HEALTH_CHECK_FAILED")
        if operation == "arm":
            trial = self.cfg.safety.attended_test
            if trial.enabled and (
                not self.test_available()
                or trial.expires_at is not None
                and trial.max_start_window_seconds is not None
                and trial.expires_at - self.wall_clock() > trial.max_start_window_seconds
            ):
                raise Rejected("ATTENDED_TEST_UNAVAILABLE")
            if (
                not self.cfg.general.simulation
                and self.cfg.safety.checks.hardware_validation
                and not self.cfg.safety.hardware_validated
                and not self.test_available()
            ):
                raise Rejected("HARDWARE_COMMISSIONING_REQUIRED")
            if self.mode != Mode.DISARMED:
                raise Rejected("INVALID_TRANSITION")
            self.last_target = q
            if trial.enabled:
                self.test_reference = vector(measured)
                if trial.gripper_baseline_limit is not None and any(
                    abs(self.test_reference[i]) > trial.gripper_baseline_limit for i in (6, 13)
                ):
                    raise Rejected("ATTENDED_TEST_GRIPPER_BASELINE")
                self.test_started, self.test_used = self.clock(), True
            self.last_tick = self.clock()
            self._transition(Mode.READY, "ARMED")
        elif operation == "start":
            if self.mode != Mode.READY and not self.operator_paused:
                raise Rejected("INVALID_TRANSITION")
            if (
                self.cfg.general.operation_mode == "hil"
                and self.cfg.recording.require_recording_for_hil
                and not self.recording
            ):
                raise Rejected("RECORDING_REQUIRED")
            self.last_target = q
            self._transition(
                Mode.RESUME if self.cfg.general.operation_mode == "hil" else Mode.TAKEOVER, "START_REQUEST"
            )
        elif operation == "toggle":
            if self.mode == Mode.POLICY:
                self._transition(Mode.TAKEOVER, "TAKEOVER_REQUEST")
            elif self.mode == Mode.HUMAN and self.cfg.general.operation_mode == "hil":
                self._transition(Mode.RESUME, "RESUME_REQUEST")
                self.resuming_from_human = True
            else:
                raise Rejected("INVALID_TRANSITION")
        elif operation == "pause":
            self.hold("OPERATOR_PAUSE")
        else:
            raise Rejected("UNKNOWN_OPERATION")

    def mode_ready(self, epoch: str, leader_pose):
        if epoch != self.epoch or self.mode not in (Mode.TAKEOVER, Mode.RESUME):
            raise Rejected("STALE_EPOCH")
        pose = vector(leader_pose)
        if (
            self.last_target is None
            or self.cfg.safety.checks.alignment
            and (
                alignment_report(pose, self.last_target, self.cfg.safety.alignment_tolerance)["error_rad"]
                > self.cfg.safety.alignment_tolerance
            )
        ):
            raise Rejected("ALIGNMENT_REQUIRED")
        if self.mode == Mode.TAKEOVER:
            # Epoch remains fixed for the prepared transaction; all pre-transition work has a different epoch.
            self.mode = Mode.HUMAN
            self.reason = "HUMAN_READY"
            self.last_command = self.clock()
            self.transition_detail = None
            self.leader_progress_at = None
            self.audit.append(
                {
                    "event": "commit",
                    "to": self.mode.value,
                    "epoch": self.epoch,
                    "reason": self.reason,
                    "monotonic": self.clock(),
                }
            )
        else:
            self.leader_ready = True
            self.transition_detail = {"epoch": self.epoch, "transition": self.mode.value,
                                      "stage": "awaiting_policy"}
            self.leader_progress_at = None

    def hold(self, reason: str):
        if self.mode not in (Mode.ESTOP, Mode.HOLD, Mode.DISARMED) or (
            self.operator_paused and reason != "OPERATOR_PAUSE"
        ):
            self._transition(Mode.HOLD, reason)

    def offer(self, packet: dict, authenticated_source: str):
        expected = "client" if packet.get("type") == "human_action" else self.cfg.policy.runner_location
        expected = "robot" if expected == "robot" else expected
        if authenticated_source != expected:
            raise Rejected("WRONG_SOURCE")
        if packet.get("epoch") != self.epoch:
            raise Rejected("STALE_EPOCH")
        human = packet.get("type") == "human_action"
        request = packet.get("type") == "policy_request"
        if human and self.mode != Mode.HUMAN:
            raise Rejected("CONTROL_NOT_GRANTED")
        if not human and not (self.mode == Mode.POLICY or self.mode == Mode.RESUME and self.leader_ready):
            raise Rejected("CONTROL_NOT_GRANTED")
        seq = packet.get("seq")
        if type(seq) is not int or seq <= self.last_seq:
            raise Rejected("STALE_SEQUENCE")
        if (
            self.cfg.safety.checks.command_timeout and self.inference_wait is not None
            and self.clock() > self.inference_wait["deadline"]
        ):
            self.hold("POLICY_INFERENCE_TIMEOUT")
            raise Rejected("POLICY_INFERENCE_TIMEOUT")
        obs_id = packet.get("observation_id")
        observed = self.observations.get(obs_id)
        max_age = (
            self.cfg.safety.observation_max_age_ms if request else
            self.cfg.safety.command_max_age_ms if human else self.cfg.policy.max_chunk_age_ms
        )
        if observed is None or (
            (request or self.cfg.safety.checks.command_freshness) and self.clock() - observed > max_age / 1000
        ):
            raise Rejected("STALE_OBSERVATION")
        if not human and self.mode == Mode.RESUME and observed < self.pending_since:
            raise Rejected("STALE_OBSERVATION")
        if packet.get("type") == "policy_error":
            error = packet.get("error")
            if not isinstance(error, dict):
                raise Rejected("INVALID_POLICY_ERROR")
            error_type = error.get("error_type")
            error_code = error.get("error_code")
            http_status = error.get("http_status")
            if (not isinstance(error_type, str) or not error_type.isascii()
                    or not error_type.isidentifier() or len(error_type) > 64
                    or (http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599))):
                raise Rejected("INVALID_POLICY_ERROR")
            if error_code not in (None, "INVALID_POLICY_IMAGE", "INVALID_DIMENSION", "INVALID_NUMBER", "POLICY_HANDSHAKE_FAILED"):
                raise Rejected("INVALID_POLICY_ERROR")
            detail = {"error_type": error_type, "http_status": http_status, "error_code": error_code}
            self.hold("POLICY_SERVICE_ERROR")
            self.policy_error = detail
            self.audit.append({"event": "policy_service_error", "error": detail, "monotonic": self.clock()})
            self.audit = self.audit[-1000:]
            return
        if request:
            if self.cfg.safety.attended_test.enabled:
                raise Rejected("ATTENDED_TEST_POLICY_NOT_ALLOWED")
            # A retry/heartbeat cannot renew a request indefinitely: an accepted
            # action must make progress before another request earns a window.
            if self.inference_wait is not None or self.inference_progress_seq == self.last_seq:
                raise Rejected("POLICY_REQUEST_NO_PROGRESS")
            now = self.clock()
            self.inference_progress_seq = self.last_seq
            self.inference_wait = {
                "seq": seq, "observation_id": obs_id, "started": now,
                "deadline": now + (self.cfg.policy.inference_wait_ms + self.cfg.safety.watchdog_ms) / 1000,
            }
            # Stop advancing the previous model target while inference blocks.
            # The driver retains the last commanded position.
            self.pending = None
            self.velocity = [0.0] * 14
            self.audit.append({"event": "policy_request_started", "epoch": self.epoch,
                               "seq": seq, "observation_id": obs_id, "monotonic": now,
                               "observation_age_ms": (now - observed) * 1000})
            self.audit = self.audit[-1000:]
            return
        if not human and self.inference_wait and (
            seq < self.inference_wait["seq"] or obs_id != self.inference_wait["observation_id"]
        ):
            raise Rejected("POLICY_REQUEST_PENDING")
        action = vector(packet.get("action"))
        if self.cfg.safety.attended_test.enabled:
            if not human or self.test_reference is None or self._test_expired():
                self.hold("ATTENDED_TEST_EXPIRED")
                raise Rejected("ATTENDED_TEST_EXPIRED")
            if (
                self.cfg.safety.attended_test.joint_delta is not None
                and self._test_distance(action) > self.cfg.safety.attended_test.joint_delta
            ):
                self.hold("ATTENDED_TEST_RANGE")
                raise Rejected("ATTENDED_TEST_RANGE")
            action = self._control_pose(action)
        low, high = self.cfg.safety.joint_min * 2, self.cfg.safety.joint_max * 2
        if self.cfg.safety.checks.position_limits and any(
            v < lo or v > hi for v, lo, hi in zip(action, low, high)
        ):
            raise Rejected("POSITION_LIMIT")
        self.last_seq = seq
        self.last_command = self.clock()
        if self.inference_wait and obs_id == self.inference_wait["observation_id"]:
            self.audit.append({"event": "policy_request_completed", "epoch": self.epoch,
                               "seq": seq, "monotonic": self.last_command,
                               "elapsed_ms": (self.last_command - self.inference_wait["started"]) * 1000})
            self.audit = self.audit[-1000:]
            self.inference_wait = None
        self.pending = (action, obs_id, seq, human)
        if self.mode == Mode.RESUME:
            self.mode, self.reason = Mode.POLICY, "POLICY_READY"
            self.transition_detail = None
            self.leader_progress_at = None
            self.release_frame = self.resuming_from_human
            self.resuming_from_human = False
            self.audit.append(
                {
                    "event": "commit",
                    "to": self.mode.value,
                    "epoch": self.epoch,
                    "reason": self.reason,
                    "monotonic": self.clock(),
                }
            )

    def tick(self, measured, healthy: bool) -> AppliedAction | None:
        now = self.clock()
        q = self._control_pose(measured)
        if self.mode in (Mode.DISARMED, Mode.HOLD, Mode.ESTOP, Mode.HOMING):
            return None
        trial = self.cfg.safety.attended_test
        if trial.enabled:
            if self.test_reference is None or self._test_expired():
                self.hold("ATTENDED_TEST_EXPIRED")
                return None
            if (
                trial.joint_delta is not None
                and self._test_distance(q) > trial.joint_delta + trial.joint_feedback_margin
            ) or (
                trial.gripper_feedback_delta is not None
                and any(
                    abs(measured[i] - self.test_reference[i]) > trial.gripper_feedback_delta for i in (6, 13)
                )
            ):
                self.hold("ATTENDED_TEST_FEEDBACK_RANGE")
                return None
        if not healthy:
            self.hold("ROBOT_HEALTH_LOST")
            return None
        if self.cfg.safety.checks.leader_heartbeat and (
            not self.leader_ok or now - self.last_leader > self.cfg.safety.leader_lease_ms / 1000
        ):
            self.hold("LEADER_LEASE_EXPIRED")
            return None
        if (
            self.cfg.safety.checks.command_timeout and self.inference_wait is not None
            and now > self.inference_wait["deadline"]
        ):
            self.hold("POLICY_INFERENCE_TIMEOUT")
            return None
        if self.mode in (Mode.TAKEOVER, Mode.RESUME, Mode.READY):
            self.last_target = q
            if (
                self.mode != Mode.READY
                and self.cfg.safety.checks.transition_timeout
                and now - self.pending_since > self.cfg.safety.transition_timeout_seconds
            ):
                self.hold("TRANSITION_TIMEOUT")
            return None
        if (
            self.cfg.safety.checks.command_timeout
            and now - self.last_command > self.cfg.safety.watchdog_ms / 1000
            and self.inference_wait is None
        ):
            self.hold("COMMAND_TIMEOUT")
            return None
        if (
            self.cfg.general.operation_mode == "hil"
            and self.cfg.recording.require_recording_for_hil
            and not self.recording
        ):
            self.hold("RECORDING_REQUIRED")
            return None
        if self.pending is None:
            return None
        target, obs_id, seq, human = self.pending
        if self.last_target is None:
            self.last_target = q
        if self.cfg.safety.checks.tracking_error:
            # Arm joints use radians. Gripper coordinates use metres and must
            # not be compared to an angular following-error threshold.
            joint = max((i for i in range(14) if i % 7 != 6),
                        key=lambda i: abs(q[i] - self.last_target[i]))
            error = abs(q[joint] - self.last_target[joint])
            safety = self.cfg.safety
            if error > safety.tracking_error_limit:
                self.tracking_since = now if self.tracking_since is None else self.tracking_since
                elapsed_ms = (now - self.tracking_since) * 1000
                hard = error > safety.tracking_error_hard_limit
                self.tracking_error_detail = {
                    "joint": self.cfg.joint_names[joint], "measured": q[joint],
                    "target": self.last_target[joint], "error_rad": error,
                    "error_degrees": math.degrees(error), "elapsed_ms": elapsed_ms,
                    "limit_rad": safety.tracking_error_limit,
                    "duration_ms": safety.tracking_error_duration_ms,
                    "hard_limit_rad": safety.tracking_error_hard_limit,
                    "trigger": "hard_limit" if hard else "sustained",
                }
                if hard or elapsed_ms + 1e-6 >= safety.tracking_error_duration_ms:
                    self.audit.append({"event": "tracking_error", **self.tracking_error_detail,
                                       "monotonic": now})
                    self.hold("TRACKING_ERROR")
                    return None
            else:
                self.tracking_since = None
                self.tracking_error_detail = None
        else:
            self.tracking_since = None
            self.tracking_error_detail = None
        # Cap dt after scheduler stalls instead of permitting a large step.
        dt = min(
            max(now - self.last_tick, 0.0001),
            self.cfg.safety.max_control_dt_periods / self.cfg.safety.control_hz,
        )
        self.last_tick = now
        output = []
        for i, (value, prev, vmax, amax) in enumerate(
            zip(
                target,
                self.last_target,
                self.cfg.safety.velocity_max * 2,
                self.cfg.safety.acceleration_max * 2,
            )
        ):
            if not self.cfg.safety.checks.velocity_limits:
                vmax = math.inf
            if not self.cfg.safety.checks.acceleration_limits:
                amax = math.inf
            if trial.enabled and trial.velocity_cap is not None:
                vmax = min(vmax, trial.velocity_cap)
            if trial.enabled and trial.acceleration_cap is not None:
                amax = min(amax, trial.acceleration_cap)
            distance = value - prev
            braking_velocity = math.inf
            if math.isfinite(amax):
                # Reserve one maximum tick of travel before braking. Using only
                # this tick's dt can overshoot when the following tick is longer.
                braking_dt = self.cfg.safety.max_control_dt_periods / self.cfg.safety.control_hz
                margin = amax * braking_dt
                energy = 2 * amax * abs(distance)
                braking_velocity = energy / (math.sqrt(margin * margin + energy) + margin)
            desired = math.copysign(min(vmax, abs(distance) / dt, braking_velocity), distance)
            velocity = max(self.velocity[i] - amax * dt, min(self.velocity[i] + amax * dt, desired))
            step = velocity * dt
            # Decelerate on target reversal; do not instantaneously zero velocity.
            position = prev + step
            if self.cfg.safety.checks.position_limits:
                lo, hi = self.cfg.safety.joint_min[i % 7], self.cfg.safety.joint_max[i % 7]
                if not lo - 1e-12 <= position <= hi + 1e-12:
                    self.limit_detail = {
                        "joint": self.cfg.joint_names[i],
                        "proposed": position,
                        "minimum": lo,
                        "maximum": hi,
                        "target": value,
                        "previous": prev,
                        "dt": dt,
                    }
                    self.audit.append({"event": "position_limit", **self.limit_detail})
                    self.hold("POSITION_LIMIT")
                    return None
                # Absorb floating-point roundoff only; never publish past a limit.
                position = max(lo, min(hi, position))
            self.velocity[i] = velocity
            output.append(position)
        if (
            trial.enabled
            and trial.joint_delta is not None
            and self._test_distance(output) > trial.joint_delta
        ):
            self.hold("ATTENDED_TEST_RANGE")
            return None
        self.last_target = output
        intervention = float(human and self.cfg.general.operation_mode == "hil")
        state = 1.0 if intervention else (2.0 if self.release_frame else 0.0)
        return AppliedAction(
            output,
            [0.0] * 14 if human else target,
            intervention,
            state,
            "human" if human else self.cfg.policy.policy_id,
            self.epoch,
            obs_id,
            seq,
            target,
        )

    @property
    def operator_paused(self):
        return self.mode == Mode.HOLD and self.reason == "OPERATOR_PAUSE"

    def status(self) -> dict:
        return {
            "mode": self.mode.value,
            "reason": self.reason,
            "operator_paused": self.operator_paused,
            "epoch": self.epoch,
            "leader_ready": self.leader_ready,
            "recording": self.recording,
            "operation_mode": self.cfg.general.operation_mode,
            "runner_location": self.cfg.policy.runner_location,
            "last_seq": self.last_seq,
            "policy_inference_waiting": self.inference_wait is not None,
            "policy_inference_elapsed_ms": (
                (self.clock() - self.inference_wait["started"]) * 1000 if self.inference_wait else None
            ),
            "policy_inference_budget_ms": self.cfg.policy.inference_wait_ms,
            "command_transport_allowance_ms": self.cfg.safety.watchdog_ms,
            "policy_error": self.policy_error,
            "velocity_limits_enabled": self.cfg.safety.checks.velocity_limits,
            "acceleration_limits_enabled": self.cfg.safety.checks.acceleration_limits,
            "position_limits_enabled": self.cfg.safety.checks.position_limits,
            "limit_detail": self.limit_detail,
            "tracking_error_detail": self.tracking_error_detail,
            "transition_detail": self.transition_status(),
        }

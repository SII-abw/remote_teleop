"""Explicit zero-return sessions, advanced by the owning control thread.

Native reset follows reset.py's reliable Int32(1) -> measured zero -> Int32(0)
contract. Without a reset topic a bounded quintic trajectory drives JointState
targets. Construction is passive; no startup or configuration read starts motion.
"""

from __future__ import annotations

import math
import threading
import time

from .control import Rejected, vector


class HomingSession:
    def __init__(self, cfg, adapter, role, guard=lambda: None, clock=time.monotonic):
        self.cfg, self.adapter, self.role, self.guard, self.clock = cfg, adapter, role, guard, clock
        self.connection = getattr(cfg, role)
        self.options = self.connection.homing
        if self.connection.backend == "sii_serial":
            raise Rejected("HOMING_ROS_REQUIRED")
        self.native = self.connection.backend == "ros" and bool(self.options.reset_topic)
        self.method = "reset_topic" if self.native else "trajectory"
        self.phase, self.state, self.progress = "NEW", "QUEUED", 0.0
        self.started = self.clock()
        self.motion_started = None
        self.last_stamp, self.confirmed = 0.0, 0
        self.last_target = None
        self.reset_pub = self.prepare_pub = None
        self.last_error = None
        self.duration = None
        self.lock = threading.RLock()
        self.trajectory_elapsed = 0.0
        self.previous_tick = None
        self.joint_residual = None
        # Leader grippers may be passive (OpenEAI kp=0); their opening must
        # never prevent six rotational joints from completing zero return.
        self.gripper_required = role != "leader"

    def _read(self):
        values, stamp = self.adapter.read()
        values = vector(values)
        # Homing always needs measured feedback, even when a teleop freshness
        # check has been disabled. Never synthesize missing joints or timestamps.
        if (
            not math.isfinite(stamp)
            or not 0 <= self.clock() - stamp <= self.cfg.safety.state_max_age_ms / 1000
        ):
            raise Rejected("HOMING_STALE_STATE")
        return values, stamp

    def _validate_pose(self, measured):
        limits = self.cfg.safety
        if any(not lo <= 0 <= hi for lo, hi in zip(limits.joint_min, limits.joint_max)):
            raise Rejected("HOMING_ZERO_OUTSIDE_LIMITS")
        start = measured.copy()
        for index in (6, 13):
            start[index] = max(limits.joint_min[6], min(limits.joint_max[6], start[index]))
        if any(
            not limits.joint_min[i % 7] <= value <= limits.joint_max[i % 7] for i, value in enumerate(start)
        ):
            raise Rejected("HOMING_POSITION_LIMIT")
        trial = limits.attended_test
        if trial.enabled:
            if self.native and (trial.velocity_cap is not None or trial.acceleration_cap is not None):
                raise Rejected("HOMING_NATIVE_LIMIT_UNVERIFIED")
            if (
                trial.joint_delta is not None
                and max(abs(start[i]) for i in range(14) if i % 7 != 6) > trial.joint_delta
            ):
                raise Rejected("ATTENDED_TEST_RANGE")
            if trial.gripper_commands is not None and any(trial.gripper_commands):
                raise Rejected("HOMING_FIXED_GRIPPER")
            if trial.gripper_feedback_delta is not None and any(
                abs(start[i]) > trial.gripper_feedback_delta for i in (6, 13)
            ):
                raise Rejected("ATTENDED_TEST_FEEDBACK_RANGE")
        return start

    def preflight(self):
        self.guard()
        measured, _ = self._read()
        start = self._validate_pose(measured)
        if not self.native:
            trial, safety = self.cfg.safety.attended_test, self.cfg.safety
            speed = [min(a, b) for a, b in zip(self.options.velocity_max, safety.velocity_max)]
            accel = [min(a, b) for a, b in zip(self.options.acceleration_max, safety.acceleration_max)]
            if trial.enabled:
                speed = [min(v, trial.velocity_cap) if trial.velocity_cap is not None else v for v in speed]
                accel = [
                    min(v, trial.acceleration_cap) if trial.acceleration_cap is not None else v for v in accel
                ]
            # max(s')=1.875, max(|s''|)=10/sqrt(3) for s=10u³−15u⁴+6u⁵.
            self.duration = max(
                self.options.duration_seconds,
                *(1.875 * abs(v) / speed[i % 7] for i, v in enumerate(start)),
                *(math.sqrt(10 / math.sqrt(3) * abs(v) / accel[i % 7]) for i, v in enumerate(start)),
            )
            if self.duration + 2 >= self.options.timeout_seconds:
                raise Rejected("HOMING_DURATION_EXCEEDS_TIMEOUT")
        return start

    def _reset(self, value):
        if self.reset_pub is not None:
            msg = self.adapter.io.Int32()
            msg.data = value
            self.reset_pub.publish(msg)

    def _write(self, target):
        self.guard()
        self._read()
        if self.role == "robot":
            self.adapter.command(target)
        else:
            self.adapter.feedback(target)
        self.last_target = target.copy()

    def _at_zero(self, values, stamp):
        if stamp <= self.last_stamp or self.motion_started is None or stamp < self.motion_started:
            return False
        self.last_stamp = stamp
        tolerance = [self.options.joint_tolerance] * 6 + [self.options.gripper_tolerance]
        zero = all(abs(value) <= tolerance[i % 7] for i, value in enumerate(values)
                   if self.gripper_required or i % 7 != 6)
        self.confirmed = self.confirmed + 1 if zero else 0
        return self.confirmed >= self.options.confirm_samples

    def tick(self):
        with self.lock:
            self._tick()

    def _tick(self):
        if self.state in ("COMPLETE", "FAILED", "CANCELLED"):
            return
        self.guard()
        if self.clock() - self.started >= self.options.timeout_seconds:
            raise Rejected("HOMING_TIMEOUT")
        measured, stamp = self._read()
        joint = max((i for i in range(14) if i % 7 != 6), key=lambda i: abs(measured[i]))
        self.joint_residual = {
            "joint": f"{'left' if joint < 7 else 'right'}.joint_{joint % 7 + 1}",
            "error_degrees": math.degrees(abs(measured[joint])),
            "tolerance_degrees": math.degrees(self.options.joint_tolerance),
        }
        if self.phase not in ("NEW", "MATCHING", "CLEARING"):
            self._validate_pose(measured)
            trial = self.cfg.safety.attended_test
            if (
                trial.enabled
                and trial.joint_delta is not None
                and max(abs(a - b) for i, (a, b) in enumerate(zip(measured, self.start_pose)) if i % 7 != 6)
                > trial.joint_delta + trial.joint_feedback_margin
            ):
                raise Rejected("ATTENDED_TEST_RANGE")
            if (
                trial.enabled
                and trial.gripper_feedback_delta is not None
                and any(abs(measured[i] - self.start_pose[i]) > trial.gripper_feedback_delta for i in (6, 13))
            ):
                raise Rejected("ATTENDED_TEST_FEEDBACK_RANGE")
        if self.phase == "NEW":
            self.start_pose = self.preflight()
            self.state = "RUNNING"
            if self.native:
                io = self.adapter.io
                self.reset_pub = io.node.create_publisher(io.Int32, self.options.reset_topic, 10)
                if self.options.reset_prepare_topic:
                    self.prepare_pub = io.node.create_publisher(
                        io.Int32, self.options.reset_prepare_topic, 10
                    )
                self.phase = "MATCHING"
            else:
                if self.role == "robot":
                    self.adapter.enable()
                else:
                    self.adapter.set_manual(False)
                # Mode acknowledgement can take time. Restart the trajectory
                # clock afterwards, recheck authorization, and sample a fresh pose.
                self.guard()
                self.start_pose = self.preflight()
                self.motion_started = self.clock()
                self.previous_tick = self.motion_started
                self.phase = "MOVING"
            return
        if self.phase == "MATCHING":
            if self.reset_pub.get_subscription_count() < self.options.reset_expected_subscribers or (
                self.prepare_pub is not None
                and self.prepare_pub.get_subscription_count() < self.options.reset_expected_subscribers
            ):
                if self.clock() - self.started > 3:
                    raise Rejected("HOMING_RESET_SUBSCRIBERS")
                return
            # Clear residual reset/prepare state only after the explicit button
            # press and after every required subscriber has matched.
            self._reset(0)
            if self.prepare_pub is not None and self.prepare_pub.get_subscription_count():
                msg = self.adapter.io.Int32()
                msg.data = 0
                self.prepare_pub.publish(msg)
            self.phase, self.cleared = "CLEARING", self.clock()
            return
        if self.phase == "CLEARING":
            if self.clock() - self.cleared >= 0.25:
                self.guard()
                if self.reset_pub.get_subscription_count() < self.options.reset_expected_subscribers:
                    raise Rejected("HOMING_RESET_SUBSCRIBERS")
                if self.role == "leader":
                    self.phase = "POSITION_MODE"
                    return
                self._reset(1)
                self.motion_started, self.phase = self.clock(), "MOVING"
            return
        if self.phase == "POSITION_MODE":
            # Reset targets are ignored by OpenEAI's MIT_DRAG controller. The
            # mode transition preloads current pose and waits for driver ACK.
            self.adapter.set_manual(False)
            self.guard()
            self._read()
            if self.reset_pub.get_subscription_count() < self.options.reset_expected_subscribers:
                raise Rejected("HOMING_RESET_SUBSCRIBERS")
            self._reset(1)
            self.motion_started, self.phase = self.clock(), "MOVING"
            return
        if self.phase == "RELEASING":
            if self.clock() - self.released >= 2.0:
                if self.reset_pub.get_subscription_count() < self.options.reset_expected_subscribers:
                    raise Rejected("HOMING_RESET_SUBSCRIBERS")
                self._reset(0)
                self._stop_targets()
                self.state, self.progress = "COMPLETE", 1.0
            return
        if self.native:
            if self.reset_pub.get_subscription_count() < self.options.reset_expected_subscribers:
                raise Rejected("HOMING_RESET_SUBSCRIBERS")
            initial = max(abs(value) for i, value in enumerate(self.start_pose) if i % 7 != 6)
            remaining = max(abs(value) for i, value in enumerate(measured) if i % 7 != 6)
            self.progress = max(self.progress, min(0.99, max(0.0, 1 - remaining / max(initial, 1e-9))))
        else:
            now = self.clock()
            self.trajectory_elapsed += min(
                max(0, now - self.previous_tick),
                self.cfg.safety.max_control_dt_periods / self.cfg.safety.control_hz,
            )
            self.previous_tick = now
            u = min(1.0, self.trajectory_elapsed / self.duration)
            scale = 10 * u**3 - 15 * u**4 + 6 * u**5
            target = [value * (1 - scale) for value in self.start_pose]
            if (
                self.last_target is not None
                and max(abs(a - b) for a, b in zip(measured, self.last_target))
                > self.cfg.safety.tracking_error_limit
            ):
                raise Rejected("HOMING_TRACKING_ERROR")
            self._write(target)
            self.progress = min(u, 0.99)
        if (self.native or self.trajectory_elapsed >= self.duration) and self._at_zero(measured, stamp):
            if self.native:
                self._reset(0)
                self.phase, self.released = "RELEASING", self.clock()
            else:
                self._stop_targets()
                self.state, self.progress = "COMPLETE", 1.0

    def _stop_targets(self):
        if self.role == "robot":
            self.adapter.stop()
        else:
            # Retain position hold; entering drag/torque-off on completion could
            # drop an unsupported arm. The scheduler stops automatic feedback.
            self.adapter.manual = True

    def cancel(self, reason="HOMING_CANCELLED", failed=False):
        with self.lock:
            self._cancel(reason, failed)

    def _cancel(self, reason, failed):
        if self.state in ("COMPLETE", "FAILED", "CANCELLED"):
            return
        errors = []
        self.state = "FAILED" if failed else "CANCELLED"
        self.last_error = reason
        try:
            if self.native:
                for _ in range(2):
                    try:
                        self._reset(0)
                    except Exception as exc:
                        errors.append(type(exc).__name__)
            elif self.role == "leader" and self.last_target is not None:
                try:
                    current, _ = self._read()
                    # Preserve gripper command units when stopping unknown bridges.
                    current[6], current[13] = self.last_target[6], self.last_target[13]
                    self.adapter.feedback(current)
                except Exception:
                    try:
                        self.adapter.feedback(self.last_target)
                    except Exception as exc:
                        errors.append(type(exc).__name__)
        finally:
            try:
                self._stop_targets()
            except Exception as exc:
                errors.append(type(exc).__name__)
            if errors:
                self.state = "FAILED"
                self.last_error = reason + "; HOMING_EXIT_FAILED: " + ",".join(errors)

    def status(self):
        return {
            "state": self.state,
            "phase": self.phase,
            "method": self.method,
            "progress": self.progress,
            "duration_seconds": self.duration,
            "error": self.last_error,
            "elapsed_ms": max(0, (self.clock() - self.started) * 1000),
            "joint_residual": self.joint_residual,
            "gripper_required": self.gripper_required,
        }

    def close(self):
        self.cancel()
        if self.native:
            node = self.adapter.io.node
            publishers = (self.reset_pub, self.prepare_pub)
            self.reset_pub = self.prepare_pub = None

            def destroy():
                for publisher in publishers:
                    if publisher is not None:
                        try:
                            node.destroy_publisher(publisher)
                        except Exception:
                            pass

            if self.state != "COMPLETE" and hasattr(node, "create_timer") and any(publishers):
                # Keep reliable reset-exit publishers alive after cancellation,
                # as reset.py does with ros2 topic pub --keep-alive.
                timer = None

                def later():
                    destroy()
                    if timer is not None:
                        timer.cancel()
                        node.destroy_timer(timer)

                timer = node.create_timer(2.0, later)
            else:
                destroy()

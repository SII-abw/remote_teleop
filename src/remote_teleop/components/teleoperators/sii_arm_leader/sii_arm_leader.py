#!/usr/bin/env python

# Copyright 2026 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

import numpy as np

from remote_teleop.components.device_types import RobotAction
from remote_teleop.components.motors import Motor, MotorNormMode
from remote_teleop.components.motors.feetech import FeetechMotorsBus, OperatingMode
from remote_teleop.components.utils.decorators import check_if_already_connected, check_if_not_connected

from ..teleoperator import Teleoperator
from .config_sii_arm_leader import SiiArmLeaderArmConfig, SiiArmLeaderConfig

logger = logging.getLogger(__name__)


class _SiiArmLeaderRuntime:
    def __init__(self, config: SiiArmLeaderArmConfig, calibration: dict):
        self.config = config
        self._manual_control_enabled = True
        self._reference_joint_angles = np.zeros(len(self.config.joint_names), dtype=np.float32)
        self._last_joint_angles = np.zeros(len(self.config.joint_names), dtype=np.float32)
        self._continuous_joint_angles = np.zeros(len(self.config.joint_names), dtype=np.float32)
        self._arm_action = np.zeros(self.config.arm_joint_count, dtype=np.float32)
        self._arm_velocity = np.zeros(self.config.arm_joint_count, dtype=np.float32)
        self._joint_offsets = np.asarray(self.config.joint_offsets, dtype=np.float32)
        self._directions = np.asarray(self.config.directions, dtype=np.float32)
        self._joint_to_motor = dict(zip(self.config.joint_names, self.config.motor_names, strict=True))

        motors = {
            motor_name: Motor(id=motor_id, model=motor_model, norm_mode=MotorNormMode.DEGREES)
            for motor_name, motor_id, motor_model in zip(
                self.config.motor_names,
                self.config.motor_ids,
                self.config.motor_models,
                strict=True,
            )
        }
        self.bus = FeetechMotorsBus(
            port=self.config.port,
            motors=motors,
            calibration=calibration,
        )

    @property
    def action_features(self) -> dict[str, type]:
        return {f"{joint_name}.pos": float for joint_name in self.config.joint_names}

    @property
    def feedback_features(self) -> dict[str, type]:
        return self.action_features

    @property
    def is_connected(self) -> bool:
        return self.bus.is_connected

    @property
    def is_calibrated(self) -> bool:
        # Relative mode uses the current physical pose as zero at connect time.
        return True

    def calibrate(self) -> None:
        logger.info("SII arm on %s uses connect-time zeroing only.", self.config.port)

    def connect(self, calibrate: bool = True) -> None:
        del calibrate
        self.bus.connect(handshake=False)
        self.bus.set_baudrate(self.config.baudrate)
        if self.config.require_motor_check:
            self.bus._assert_motors_exist()
        self.configure()
        self._capture_reference_pose()
        logger.info("%s connected.", self)

    def configure(self) -> None:
        self.bus.disable_torque()
        self.bus.configure_motors()
        for motor in self.bus.motors:
            self.bus.write("Operating_Mode", motor, OperatingMode.POSITION.value)
        self._manual_control_enabled = True
        self.bus.disable_torque()

    @staticmethod
    def _wrap_to_pi(angle_rad: float) -> float:
        return float((angle_rad + np.pi) % (2 * np.pi) - np.pi)

    def _motor_resolution(self, motor_name: str) -> int:
        return int(self.bus.model_resolution_table[self.bus.motors[motor_name].model])

    def _raw_to_radians(self, motor_name: str, raw_position: float | int) -> float:
        resolution = self._motor_resolution(motor_name)
        return float(raw_position) * (2.0 * np.pi) / float(resolution)

    def _radians_to_raw(self, motor_name: str, angle_rad: float) -> int:
        resolution = self._motor_resolution(motor_name)
        return int(round(float(angle_rad) * float(resolution) / (2.0 * np.pi)))

    def _capture_reference_pose(self) -> None:
        raw_positions = self.bus.sync_read("Present_Position", normalize=False)
        self._reference_joint_angles = np.asarray(
            [
                self._raw_to_radians(self._joint_to_motor[joint_name], raw_positions[self._joint_to_motor[joint_name]])
                for joint_name in self.config.joint_names
            ],
            dtype=np.float32,
        )
        self._last_joint_angles = self._reference_joint_angles.copy()
        self._continuous_joint_angles = self._reference_joint_angles.copy()
        self._arm_action.fill(0.0)
        self._arm_velocity.fill(0.0)

    def _reference_feedback_pose(self) -> RobotAction:
        pose = {
            f"{joint_name}.pos": float(self._joint_offsets[index])
            for index, joint_name in enumerate(self.config.joint_names[: self.config.arm_joint_count])
        }
        gripper_alpha_at_reference = 1.0 if self._directions[-1] < 0 else 0.0
        pose[f"{self.config.joint_names[-1]}.pos"] = float(
            self.config.gripper_output_scale * gripper_alpha_at_reference
            + self.config.gripper_output_offset
        )
        return pose

    @check_if_not_connected
    def capture_reference_pose(self) -> None:
        self._capture_reference_pose()

    @check_if_not_connected
    def get_reference_feedback_pose(self) -> RobotAction:
        return self._reference_feedback_pose()

    @check_if_not_connected
    def set_manual_control(self, enabled: bool) -> None:
        if enabled:
            if not self._manual_control_enabled:
                self.bus.disable_torque()
                self._manual_control_enabled = True
            return

        if self._manual_control_enabled:
            self.bus.enable_torque()
            self._manual_control_enabled = False

    @check_if_not_connected
    def get_action(self) -> RobotAction:
        raw_positions = self.bus.sync_read("Present_Position", normalize=False, num_retry=2)
        joint_angles = np.asarray(
            [
                self._raw_to_radians(self._joint_to_motor[joint_name], raw_positions[self._joint_to_motor[joint_name]])
                for joint_name in self.config.joint_names
            ],
            dtype=np.float32,
        )

        incremental_deltas = np.asarray(
            [
                self._wrap_to_pi(current - previous)
                for current, previous in zip(joint_angles, self._last_joint_angles, strict=True)
            ],
            dtype=np.float32,
        )
        self._continuous_joint_angles += incremental_deltas
        self._last_joint_angles = joint_angles
        deltas = (self._continuous_joint_angles - self._reference_joint_angles) * self._directions

        arm_action = deltas[: self.config.arm_joint_count]
        if self.config.use_degrees:
            arm_action = np.rad2deg(arm_action)

        target_velocity = arm_action - self._arm_action
        alpha = float(self.config.smoothing)
        self._arm_velocity = alpha * self._arm_velocity + (1.0 - alpha) * target_velocity
        self._arm_action = self._arm_action + self._arm_velocity
        arm_output = self._arm_action + self._joint_offsets

        gripper_delta_deg = abs(np.rad2deg(float(deltas[-1])))
        gripper_alpha = np.clip(gripper_delta_deg / self.config.gripper_max_delta_deg, 0.0, 1.0)
        if self._directions[-1] < 0:
            gripper_alpha = 1.0 - gripper_alpha
        gripper_output = (
            self.config.gripper_output_scale * gripper_alpha + self.config.gripper_output_offset
        )

        action_values = [*arm_output.tolist(), float(gripper_output)]
        return {
            f"{joint_name}.pos": float(value)
            for joint_name, value in zip(self.config.joint_names, action_values, strict=True)
        }

    @check_if_not_connected
    def send_feedback(self, feedback: dict[str, float]) -> None:
        goal_positions: dict[str, int] = {}
        feedback_keys = {key: float(value) for key, value in feedback.items() if key.endswith(".pos")}
        if not feedback_keys:
            return

        arm_action = self._arm_action.copy()
        for index, joint_name in enumerate(self.config.joint_names[: self.config.arm_joint_count]):
            key = f"{joint_name}.pos"
            if key not in feedback_keys:
                continue
            target = feedback_keys[key] - float(self._joint_offsets[index])
            arm_action[index] = target
            if self.config.use_degrees:
                target = float(np.deg2rad(target))
            desired_angle = self._reference_joint_angles[index] + target / float(self._directions[index])
            motor_name = self._joint_to_motor[joint_name]
            goal_positions[motor_name] = self._radians_to_raw(motor_name, desired_angle)

        gripper_key = f"{self.config.joint_names[-1]}.pos"
        if gripper_key in feedback_keys:
            gripper_alpha = (
                feedback_keys[gripper_key] - self.config.gripper_output_offset
            ) / self.config.gripper_output_scale
            gripper_alpha = float(np.clip(gripper_alpha, 0.0, 1.0))
            if self._directions[-1] < 0:
                gripper_alpha = 1.0 - gripper_alpha
            gripper_delta_rad = np.deg2rad(gripper_alpha * self.config.gripper_max_delta_deg)
            desired_angle = self._reference_joint_angles[-1] + float(np.sign(self._directions[-1])) * float(
                gripper_delta_rad
            )
            gripper_motor = self._joint_to_motor[self.config.joint_names[-1]]
            goal_positions[gripper_motor] = self._radians_to_raw(gripper_motor, desired_angle)

        if goal_positions:
            self.set_manual_control(False)
            self.bus.sync_write("Goal_Position", goal_positions, normalize=False)
            self._arm_action = arm_action
            self._arm_velocity.fill(0.0)

    @check_if_not_connected
    def disconnect(self) -> None:
        self.bus.disconnect(self.config.disable_torque_on_disconnect)
        logger.info("SII arm on %s disconnected.", self.config.port)


class SiiArmLeader(Teleoperator):
    """Feetech-backed SII leader arm(s) with the same relative action semantics as `GelloLeader`."""

    config_class = SiiArmLeaderConfig
    name = "sii_arm_leader"

    def __init__(self, config: SiiArmLeaderConfig):
        super().__init__(config)
        self.config = config
        self._single_arm_mode = not bool(config.arms)
        self._arms = self._build_arm_runtimes(config)

    def _legacy_arm_config(self, config: SiiArmLeaderConfig) -> SiiArmLeaderArmConfig:
        if config.port is None:
            raise ValueError("`port` is required when `arms` is not provided.")
        return SiiArmLeaderArmConfig(
            port=config.port,
            baudrate=config.baudrate,
            joint_names=config.joint_names,
            motor_names=config.motor_names,
            motor_ids=config.motor_ids,
            motor_models=config.motor_models,
            directions=config.directions,
            arm_joint_count=config.arm_joint_count,
            joint_offsets=config.joint_offsets,
            smoothing=config.smoothing,
            use_degrees=config.use_degrees,
            gripper_max_delta_deg=config.gripper_max_delta_deg,
            gripper_output_scale=config.gripper_output_scale,
            gripper_output_offset=config.gripper_output_offset,
            disable_torque_on_disconnect=config.disable_torque_on_disconnect,
            require_motor_check=config.require_motor_check,
            full_turn_motors=config.full_turn_motors,
            gripper_motor=config.gripper_motor,
        )

    def _build_arm_runtimes(self, config: SiiArmLeaderConfig) -> dict[str, _SiiArmLeaderRuntime]:
        arm_configs = config.arms if config.arms else {"": self._legacy_arm_config(config)}
        runtimes = {}
        feature_keys: set[str] = set()
        for arm_name, arm_config in arm_configs.items():
            for joint_name in arm_config.joint_names:
                feature_key = self._feature_key(arm_name, joint_name)
                if feature_key in feature_keys:
                    raise ValueError(f"Duplicate SII leader feature key: {feature_key}")
                feature_keys.add(feature_key)
            runtimes[arm_name] = _SiiArmLeaderRuntime(arm_config, self.calibration)
        return runtimes

    def _feature_key(self, arm_name: str, joint_name: str) -> str:
        if self._single_arm_mode:
            return f"{joint_name}.pos"
        return f"{arm_name}_{joint_name}.pos"

    def _unprefix_feedback_key(self, arm_name: str, key: str) -> str | None:
        if not key.endswith(".pos"):
            return None
        if self._single_arm_mode:
            return key
        prefix = f"{arm_name}_"
        if not key.startswith(prefix):
            return None
        return f"{key.removeprefix(prefix).removesuffix('.pos')}.pos"

    @property
    def bus(self):
        if not self._single_arm_mode:
            raise AttributeError("`bus` is only available in single-arm SII leader mode.")
        return next(iter(self._arms.values())).bus

    @property
    def action_features(self) -> dict[str, type]:
        return {
            self._feature_key(arm_name, joint_name): float
            for arm_name, arm in self._arms.items()
            for joint_name in arm.config.joint_names
        }

    @property
    def feedback_features(self) -> dict[str, type]:
        return self.action_features

    @property
    def is_connected(self) -> bool:
        return all(arm.is_connected for arm in self._arms.values())

    @property
    def is_calibrated(self) -> bool:
        return all(arm.is_calibrated for arm in self._arms.values())

    def calibrate(self) -> None:
        for arm in self._arms.values():
            arm.calibrate()

    @check_if_not_connected
    def capture_reference_pose(self) -> None:
        for arm in self._arms.values():
            arm.capture_reference_pose()

    @check_if_not_connected
    def get_reference_feedback_pose(self) -> RobotAction:
        pose: RobotAction = {}
        for arm_name, arm in self._arms.items():
            arm_pose = arm.get_reference_feedback_pose()
            pose.update(
                {
                    self._feature_key(arm_name, key.removesuffix(".pos")): value
                    for key, value in arm_pose.items()
                    if key.endswith(".pos")
                }
            )
        return pose

    @check_if_already_connected
    def connect(self, calibrate: bool = True) -> None:
        try:
            for arm in self._arms.values():
                arm.connect(calibrate)
        except Exception:
            self.disconnect()
            raise
        logger.info("%s connected.", self)

    def configure(self) -> None:
        for arm in self._arms.values():
            arm.configure()

    @check_if_not_connected
    def set_manual_control(self, enabled: bool) -> None:
        for arm in self._arms.values():
            arm.set_manual_control(enabled)

    @check_if_not_connected
    def get_action(self) -> RobotAction:
        action: RobotAction = {}
        for arm_name, arm in self._arms.items():
            arm_action = arm.get_action()
            action.update(
                {
                    self._feature_key(arm_name, key.removesuffix(".pos")): value
                    for key, value in arm_action.items()
                    if key.endswith(".pos")
                }
            )
        return action

    @check_if_not_connected
    def send_feedback(self, feedback: dict[str, float]) -> None:
        for arm_name, arm in self._arms.items():
            arm_feedback = {}
            for key, value in feedback.items():
                unprefixed_key = self._unprefix_feedback_key(arm_name, key)
                if unprefixed_key is not None:
                    arm_feedback[unprefixed_key] = value
            arm.send_feedback(arm_feedback)

    def disconnect(self) -> None:
        for arm in self._arms.values():
            if arm.is_connected:
                arm.disconnect()
        logger.info("%s disconnected.", self)

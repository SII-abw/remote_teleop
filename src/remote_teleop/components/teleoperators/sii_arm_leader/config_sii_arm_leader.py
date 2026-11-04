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

from dataclasses import dataclass, field

from ..config import TeleoperatorConfig


@dataclass
class SiiArmLeaderArmConfig:
    port: str
    baudrate: int = 1_000_000
    joint_names: list[str] = field(
        default_factory=lambda: [f"joint_{index}" for index in range(1, 7)] + ["gripper"]
    )
    motor_names: list[str] = field(
        default_factory=lambda: [
            "base_yaw",
            "shoulder_pitch",
            "elbow_pitch",
            "forearm_roll",
            "wrist_pitch",
            "wrist_roll",
            "gripper",
        ]
    )
    motor_ids: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    motor_models: list[str] = field(default_factory=lambda: ["sts3215"] * 7)
    directions: list[float] = field(default_factory=lambda: [-1.0, -1.0, 1.0, 1.0, -1.0, -1.0, 1.0])
    arm_joint_count: int = 6
    joint_offsets: list[float] = field(default_factory=lambda: [0.0] * 6)
    smoothing: float = 0.5
    use_degrees: bool = False
    gripper_max_delta_deg: float = 40.0
    gripper_output_scale: float = 0.085
    gripper_output_offset: float = -0.005
    disable_torque_on_disconnect: bool = True
    require_motor_check: bool = True
    full_turn_motors: list[str] = field(default_factory=lambda: ["wrist_roll"])
    gripper_motor: str = "gripper"

    def __post_init__(self) -> None:
        expected_joint_count = self.arm_joint_count + 1
        if len(self.joint_names) != expected_joint_count:
            raise ValueError(
                f"`joint_names` must contain {expected_joint_count} entries "
                f"({self.arm_joint_count} arm joints + 1 gripper)."
            )
        if len(self.motor_names) != len(self.joint_names):
            raise ValueError("`motor_names` must have the same length as `joint_names`.")
        if len(self.motor_ids) != len(self.joint_names):
            raise ValueError("`motor_ids` must have the same length as `joint_names`.")
        if len(self.motor_models) != len(self.joint_names):
            raise ValueError("`motor_models` must have the same length as `joint_names`.")
        if len(self.directions) != len(self.joint_names):
            raise ValueError("`directions` must have the same length as `joint_names`.")
        if len(self.joint_offsets) != self.arm_joint_count:
            raise ValueError("`joint_offsets` must have the same length as the arm joint count.")
        if len(set(self.motor_names)) != len(self.motor_names):
            raise ValueError("`motor_names` must be unique.")
        if self.gripper_motor not in self.motor_names:
            raise ValueError("`gripper_motor` must be present in `motor_names`.")
        if any(direction == 0 for direction in self.directions):
            raise ValueError("`directions` entries must be non-zero.")
        if not 0.0 <= self.smoothing <= 1.0:
            raise ValueError("`smoothing` must be in the [0, 1] range.")
        if self.gripper_max_delta_deg <= 0:
            raise ValueError("`gripper_max_delta_deg` must be > 0.")
        if self.gripper_output_scale == 0:
            raise ValueError("`gripper_output_scale` must be non-zero.")
        unknown_full_turn = sorted(set(self.full_turn_motors) - set(self.motor_names))
        if unknown_full_turn:
            raise ValueError(f"Unknown `full_turn_motors`: {unknown_full_turn}")


@TeleoperatorConfig.register_subclass("sii_arm_leader")
@dataclass
class SiiArmLeaderConfig(TeleoperatorConfig):
    """Configuration for SII leader teleoperators.

    By default this keeps the historical single-arm configuration. For multiple arms, set
    `arms`, where each arm name is used as a feature prefix (for example `left_joint_1.pos`).
    """

    port: str | None = None
    baudrate: int = 1_000_000
    joint_names: list[str] = field(
        default_factory=lambda: [f"joint_{index}" for index in range(1, 7)] + ["gripper"]
    )
    motor_names: list[str] = field(
        default_factory=lambda: [
            "base_yaw",
            "shoulder_pitch",
            "elbow_pitch",
            "forearm_roll",
            "wrist_pitch",
            "wrist_roll",
            "gripper",
        ]
    )
    motor_ids: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    motor_models: list[str] = field(default_factory=lambda: ["sts3215"] * 7)
    directions: list[float] = field(default_factory=lambda: [-1.0, -1.0, 1.0, 1.0, -1.0, -1.0, 1.0])
    arm_joint_count: int = 6
    joint_offsets: list[float] = field(default_factory=lambda: [0.0] * 6)
    smoothing: float = 0.5
    use_degrees: bool = False
    gripper_max_delta_deg: float = 40.0
    gripper_output_scale: float = 0.085
    gripper_output_offset: float = -0.005
    disable_torque_on_disconnect: bool = True
    require_motor_check: bool = True
    full_turn_motors: list[str] = field(default_factory=lambda: ["wrist_roll"])
    gripper_motor: str = "gripper"
    arms: dict[str, SiiArmLeaderArmConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.arms:
            if any(not arm_name for arm_name in self.arms):
                raise ValueError("`arms` keys must be non-empty arm names.")
            if any("." in arm_name for arm_name in self.arms):
                raise ValueError("`arms` keys must not contain '.'.")
            self.arms = {
                arm_name: arm_config
                if isinstance(arm_config, SiiArmLeaderArmConfig)
                else SiiArmLeaderArmConfig(**arm_config)
                for arm_name, arm_config in self.arms.items()
            }
            for arm_config in self.arms.values():
                arm_config.__post_init__()
            return

        if self.port is None:
            raise ValueError("`port` is required when `arms` is not provided.")
        SiiArmLeaderArmConfig(
            port=self.port,
            baudrate=self.baudrate,
            joint_names=self.joint_names,
            motor_names=self.motor_names,
            motor_ids=self.motor_ids,
            motor_models=self.motor_models,
            directions=self.directions,
            arm_joint_count=self.arm_joint_count,
            joint_offsets=self.joint_offsets,
            smoothing=self.smoothing,
            use_degrees=self.use_degrees,
            gripper_max_delta_deg=self.gripper_max_delta_deg,
            gripper_output_scale=self.gripper_output_scale,
            gripper_output_offset=self.gripper_output_offset,
            disable_torque_on_disconnect=self.disable_torque_on_disconnect,
            require_motor_check=self.require_motor_check,
            full_turn_motors=self.full_turn_motors,
            gripper_motor=self.gripper_motor,
        )

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlparse, urlunparse

import numpy as np
import torch

from remote_teleop.components.utils.constants import OBS_IMAGES, OBS_STATE


def _require_msgpack():
    try:
        import msgpack
    except ImportError as err:
        raise ImportError(
            "Remote OpenPI inference requires the `msgpack` package. Install it with `pip install msgpack`."
        ) from err
    return msgpack


def _pack_array(obj: Any) -> Any:
    if isinstance(obj, (np.ndarray, np.generic)) and obj.dtype.kind in ("V", "O", "c"):
        raise ValueError(f"Unsupported dtype for MsgPack ndarray transport: {obj.dtype}")
    if isinstance(obj, np.ndarray):
        array = np.ascontiguousarray(obj)
        return {
            b"__ndarray__": True,
            b"data": array.tobytes(),
            b"dtype": array.dtype.str,
            b"shape": array.shape,
        }
    if isinstance(obj, np.generic):
        return {b"__npgeneric__": True, b"data": obj.item(), b"dtype": obj.dtype.str}
    return obj


def _unpack_array(obj: dict[Any, Any]) -> Any:
    if b"__ndarray__" in obj:
        return np.ndarray(buffer=obj[b"data"], dtype=np.dtype(obj[b"dtype"]), shape=obj[b"shape"])
    if b"__npgeneric__" in obj:
        return np.dtype(obj[b"dtype"]).type(obj[b"data"])
    return obj


def _packb(data: dict[str, Any]) -> bytes:
    msgpack = _require_msgpack()
    return msgpack.packb(data, default=_pack_array, use_bin_type=True)


def _unpackb(data: bytes) -> Any:
    msgpack = _require_msgpack()
    return msgpack.unpackb(data, object_hook=_unpack_array, raw=False)


@dataclass
class _InterventionFeedbackRequest:
    """One sampled human transition sent outside the real-time control path."""

    observation_frame: dict[str, np.ndarray]
    action_dim: int
    task: str | None
    robot_type: str | None
    episode_id: str
    executed_action: np.ndarray
    is_intervention: bool
    rlt_switch: bool


@dataclass
class RemotePolicyInferenceConfig:
    enable: bool = False
    # OpenPI WebsocketPolicyServer endpoint.
    url: str = "ws://127.0.0.1:8000"
    timeout_s: float = 10.0
    policy_id: str | None = None
    api_key: str | None = None

    # OpenPI commonly expects slash-style observation keys and a `prompt`.
    # `pi05` matches the lightweight ROS client payload used by arx_pi05_ros_inference.py.
    request_format: str = "openpi"  # "openpi", "lerobot", or "pi05"
    prompt_key: str = "prompt"
    state_source_key: str = OBS_STATE
    state_target_key: str = "observation/state"
    include_top_level_state: bool = True
    image_key_map: dict[str, str] = field(default_factory=dict)
    include_top_level_images: bool = True
    top_level_image_format: str = "franka"  # "franka" or "aliases"
    include_lerobot_keys: bool = False
    include_task_key: bool = False
    include_robot_type_key: bool = False
    extra_payload: dict[str, str] = field(default_factory=dict)

    # Optional adversarial token appended to the prompt sent to the inference server.
    # This token is NOT saved in the dataset — it only affects the server-side prompt.
    # When set, the effective prompt sent to the server becomes: `<task> <adv_token>`.
    adv_token: str | None = None

    # OpenPI usually returns an action chunk. Queueing mirrors local chunking policies.
    queue_actions: bool = True
    max_queued_actions: int | None = None
    # Optional ordinary-VLA endpoint used outside an explicitly activated RLT
    # segment.  The primary ``url`` remains the synchronous Stage2 endpoint.
    # This split lets a long task retain OpenPI's normal action-chunk queue and
    # contact the learner only for the critical phase selected by the operator.
    vla_url: str | None = None
    vla_max_queued_actions: int = 49
    action_key: str = "actions"
    action_index: int = 0
    # Absolute action envelope applied after deserializing every server
    # response and before queueing or robot command construction.  For the
    # dual ARX policy this is joint radians plus gripper width in metres.
    action_low: list[float] | None = None
    action_high: list[float] | None = None

    # RLT Stage 2 turns the usual inference socket into a bidirectional
    # environment protocol.  Every observation reports the action that was
    # actually executed during the preceding control cycle.  Keep this opt-in
    # so ordinary OpenPI serving remains byte-for-byte compatible.
    rlt_stage2_transition_feedback: bool = False
    executed_action_tolerance: float = 1.0e-5
    rlt_segment_key: str = "r"
    hold_on_rlt_failure: bool = True

    # Read-only HTTP monitor. It consumes observations already acquired by the
    # robot loop and never opens a camera or exposes control endpoints.
    monitor_enable: bool = False
    monitor_host: str = "127.0.0.1"
    monitor_port: int = 8765
    monitor_image_fps: float = 6.0
    monitor_jpeg_quality: int = 80

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ValueError("`remote_policy.timeout_s` must be > 0.")
        if self.request_format not in {"openpi", "lerobot", "pi05"}:
            raise ValueError("`remote_policy.request_format` must be one of {'openpi', 'lerobot', 'pi05'}.")
        if self.top_level_image_format not in {"franka", "aliases"}:
            raise ValueError("`remote_policy.top_level_image_format` must be one of {'franka', 'aliases'}.")
        if self.action_index < 0:
            raise ValueError("`remote_policy.action_index` must be >= 0.")
        if self.max_queued_actions is not None and self.max_queued_actions <= 0:
            raise ValueError("`remote_policy.max_queued_actions` must be > 0 when set.")
        if self.vla_max_queued_actions <= 0:
            raise ValueError("`remote_policy.vla_max_queued_actions` must be > 0.")
        if self.vla_url is not None and not self.rlt_stage2_transition_feedback:
            raise ValueError(
                "`remote_policy.vla_url` is only valid with "
                "`remote_policy.rlt_stage2_transition_feedback=true`."
            )
        if self.vla_url is not None and self.vla_url.rstrip("/") == self.url.rstrip("/"):
            raise ValueError(
                "`remote_policy.vla_url` and `remote_policy.url` must point to "
                "different Stage1 and Stage2 endpoints."
            )
        if (self.action_low is None) != (self.action_high is None):
            raise ValueError(
                "`remote_policy.action_low` and `remote_policy.action_high` must be set together."
            )
        if self.action_low is not None:
            low = np.asarray(self.action_low, dtype=np.float32).reshape(-1)
            high = np.asarray(self.action_high, dtype=np.float32).reshape(-1)
            if low.size == 0 or low.shape != high.shape:
                raise ValueError("Remote policy action bounds must be non-empty and have equal shapes.")
            if not np.all(np.isfinite(low)) or not np.all(np.isfinite(high)):
                raise ValueError("Remote policy action bounds must be finite.")
            if not np.all(low < high):
                raise ValueError("Every remote policy action_low value must be below action_high.")
        if self.executed_action_tolerance < 0:
            raise ValueError("`remote_policy.executed_action_tolerance` must be >= 0.")
        if len(self.rlt_segment_key) != 1:
            raise ValueError("`remote_policy.rlt_segment_key` must be one character.")
        if not self.monitor_host:
            raise ValueError("`remote_policy.monitor_host` must be non-empty.")
        if not 0 < self.monitor_port < 65536:
            raise ValueError("`remote_policy.monitor_port` must be between 1 and 65535.")
        if self.monitor_image_fps <= 0:
            raise ValueError("`remote_policy.monitor_image_fps` must be > 0.")
        if not 1 <= self.monitor_jpeg_quality <= 100:
            raise ValueError("`remote_policy.monitor_jpeg_quality` must be between 1 and 100.")
        if self.rlt_stage2_transition_feedback and self.queue_actions:
            raise ValueError(
                "RLT Stage 2 requires `remote_policy.queue_actions=false` so every "
                "executed action is acknowledged before the next action is requested."
            )


class OpenPIWebsocketRemotePolicy:
    def __init__(self, cfg: RemotePolicyInferenceConfig):
        _require_msgpack()
        self.cfg = cfg
        self._action_queue: deque[np.ndarray] = deque()
        self._ws: Any | None = None
        self._server_metadata: dict[str, Any] = {}
        self._episode_id: str | None = None
        self._episode_step = 0
        self._episode_start_pending = False
        self._pending_executed_action: np.ndarray | None = None
        self._pending_is_intervention = False
        self._pending_transition_in_segment = False
        self._rlt_segment_active = False
        self._rlt_segment_has_action = False
        self._last_requested_segment_active = False
        self._pending_segment_success: bool | None = None
        self._last_observation_frame: dict[str, np.ndarray] | None = None
        self._last_control_tick_t: float | None = None
        self._control_hz_ema: float | None = None
        self._intervention_feedback_condition = threading.Condition()
        self._intervention_feedback_pending: _InterventionFeedbackRequest | None = None
        self._intervention_feedback_inflight = False
        self._intervention_feedback_error: BaseException | None = None
        self._intervention_feedback_thread: threading.Thread | None = None
        self._intervention_feedback_coalesced = 0
        self._monitor: Any | None = None
        self._vla_policy: OpenPIWebsocketRemotePolicy | None = None
        if cfg.vla_url is not None:
            vla_cfg = replace(
                cfg,
                url=cfg.vla_url,
                vla_url=None,
                queue_actions=True,
                max_queued_actions=cfg.vla_max_queued_actions,
                rlt_stage2_transition_feedback=False,
                monitor_enable=False,
            )
            self._vla_policy = OpenPIWebsocketRemotePolicy(vla_cfg)
        if cfg.monitor_enable:
            from remote_teleop.components.remote_policy.rlt_monitor import RLTMonitorServer

            self._monitor = RLTMonitorServer(
                host=cfg.monitor_host,
                port=cfg.monitor_port,
                image_fps=cfg.monitor_image_fps,
                jpeg_quality=cfg.monitor_jpeg_quality,
            )
            self._monitor_status(
                last_event="client initialized",
                task="",
            )

    @property
    def server_metadata(self) -> dict[str, Any]:
        return self._server_metadata

    def reset(self) -> None:
        """Clear a cached action chunk without changing Stage 2 episode state."""
        self._action_queue.clear()
        if self._vla_policy is not None:
            self._vla_policy.reset()

    @property
    def requires_continuous_observation(self) -> bool:
        """Whether policy inference must continue while a human intervenes."""
        if self._vla_policy is None:
            return self.cfg.rlt_stage2_transition_feedback
        return bool(
            self._rlt_segment_active or self._pending_segment_success is not None
        )

    def submit_intervention_feedback(
        self,
        *,
        observation_frame: dict[str, np.ndarray],
        action_dim: int,
        task: str | None,
        robot_type: str | None,
    ) -> bool:
        """Queue the latest human transition without blocking robot control.

        Only one not-yet-started request is retained. If inference is slower
        than the control loop, a newer observation replaces that pending
        request. The request already being sent is never interrupted, and all
        WebSocket access remains serialized on the feedback worker.
        """
        if not self.cfg.rlt_stage2_transition_feedback:
            return False
        if (
            self._episode_id is None
            or self._pending_executed_action is None
            or self._pending_segment_success is not None
        ):
            return False

        with self._intervention_feedback_condition:
            if self._intervention_feedback_error is not None:
                return False

            request = _InterventionFeedbackRequest(
                observation_frame=self._copy_observation_frame(observation_frame),
                action_dim=int(action_dim),
                task=task,
                robot_type=robot_type,
                episode_id=self._episode_id,
                executed_action=self._pending_executed_action.copy(),
                is_intervention=bool(self._pending_is_intervention),
                rlt_switch=bool(self._rlt_segment_active),
            )
            self._pending_executed_action = None
            self._pending_is_intervention = False
            self._pending_transition_in_segment = False
            self._last_requested_segment_active = request.rlt_switch
            self._rlt_segment_has_action = bool(
                self._rlt_segment_has_action or request.rlt_switch
            )

            if self._intervention_feedback_pending is not None:
                self._intervention_feedback_coalesced += 1
            self._intervention_feedback_pending = request
            if self._intervention_feedback_thread is None:
                self._intervention_feedback_thread = threading.Thread(
                    target=self._intervention_feedback_loop,
                    name="rlt-intervention-feedback",
                    daemon=True,
                )
                self._intervention_feedback_thread.start()
            self._intervention_feedback_condition.notify_all()
        return True

    def wait_for_intervention_feedback(self) -> None:
        """Drain asynchronous feedback before synchronous policy inference."""
        with self._intervention_feedback_condition:
            while (
                self._intervention_feedback_pending is not None
                or self._intervention_feedback_inflight
            ):
                self._intervention_feedback_condition.wait(timeout=0.1)
            if self._intervention_feedback_error is not None:
                error = self._intervention_feedback_error
                self._intervention_feedback_error = None
                raise ConnectionError(
                    "Asynchronous Stage 2 intervention feedback failed."
                ) from error

    def _intervention_feedback_loop(self) -> None:
        while True:
            with self._intervention_feedback_condition:
                while self._intervention_feedback_pending is None:
                    self._intervention_feedback_condition.wait()
                request = self._intervention_feedback_pending
                self._intervention_feedback_pending = None
                self._intervention_feedback_inflight = True

            try:
                self._send_intervention_feedback(request)
            except Exception as error:
                logging.exception(
                    "Asynchronous Stage 2 intervention feedback failed; "
                    "human control remains local."
                )
                with self._intervention_feedback_condition:
                    self._intervention_feedback_error = error
                    self._intervention_feedback_pending = None
            finally:
                with self._intervention_feedback_condition:
                    self._intervention_feedback_inflight = False
                    self._intervention_feedback_condition.notify_all()

    def _send_intervention_feedback(
        self, request: _InterventionFeedbackRequest
    ) -> None:
        request_started_t = time.perf_counter()
        payload = self._build_payload(
            request.observation_frame,
            task=request.task,
            robot_type=request.robot_type,
        )
        payload["_rlt_stage2"] = {
            "episode_id": request.episode_id,
            "step_id": self._episode_step,
            "episode_start": self._episode_start_pending,
            "executed_action": request.executed_action,
            "reward": 0.0,
            "terminated": False,
            "truncated": False,
            "is_intervention": request.is_intervention,
            "rlt_switch": request.rlt_switch,
        }
        self._infer(payload)
        self._episode_start_pending = False
        self._last_observation_frame = request.observation_frame
        self._episode_step += 1
        self._monitor_status(
            control_mode="HUMAN",
            rlt_segment_state="ACTIVE" if request.rlt_switch else "INACTIVE",
            action_source="HUMAN TELEOP",
            inference_latency_ms=(time.perf_counter() - request_started_t) * 1000.0,
            stage2_connected=self._ws is not None,
            stage2_episode_id=request.episode_id,
            stage2_step=self._episode_step,
            intervention_feedback_coalesced=self._intervention_feedback_coalesced,
            last_error=None,
        )

    @staticmethod
    def _copy_observation_frame(
        observation_frame: dict[str, np.ndarray],
    ) -> dict[str, np.ndarray]:
        return {
            key: np.array(value, copy=True)
            if isinstance(value, (np.ndarray, np.generic))
            else value
            for key, value in observation_frame.items()
        }

    @property
    def rlt_segment_active(self) -> bool:
        """Whether newly requested actions should use the RLT critical phase."""
        return self._rlt_segment_active

    @property
    def hold_on_rlt_failure(self) -> bool:
        """Whether the recording loop should hold after a failed RLT segment."""
        return bool(self.cfg.hold_on_rlt_failure)

    def start_episode(self) -> None:
        """Start a new robot episode for the RLT Stage 2 environment protocol."""
        self.reset()
        self._monitor_status(
            control_mode="VLA" if self._vla_policy is not None else "RLT",
            rlt_segment_state="INACTIVE",
            stage2_episode_id=None,
            stage2_step=0,
            last_event="outer task episode started",
            last_error=None,
        )
        if not self.cfg.rlt_stage2_transition_feedback:
            return
        if self._pending_executed_action is not None:
            logging.warning(
                "Dropping an unflushed Stage 2 transition while starting a new episode."
            )
        # In dual-path mode the long task starts on the ordinary VLA server.
        # A remote Stage2 Gym episode is created lazily only when `r` selects a
        # critical segment.
        if self._vla_policy is not None:
            self._clear_stage2_episode()
            return
        self._episode_id = uuid.uuid4().hex
        self._episode_step = 0
        self._episode_start_pending = True
        self._pending_executed_action = None
        self._pending_is_intervention = False
        self._pending_transition_in_segment = False
        self._rlt_segment_active = False
        self._rlt_segment_has_action = False
        self._last_requested_segment_active = False
        self._pending_segment_success = None
        self._last_observation_frame = None

    def start_rlt_segment(self) -> bool:
        """Route subsequent actions through the RLT critical-phase policy."""
        if not self.cfg.rlt_stage2_transition_feedback:
            return False
        if self._episode_id is None:
            if self._vla_policy is None:
                raise RuntimeError("Cannot start an RLT segment before `start_episode()`.")
            self._begin_protocol_episode()
        if self._pending_segment_success is not None:
            logging.warning("RLT segment start ignored while its terminal is pending.")
            return False
        if self._rlt_segment_active:
            logging.info("RLT segment start ignored because Stage2 is already active.")
            return False
        # Drop any remaining ordinary-VLA chunk.  The first action following
        # `r` must be computed synchronously by the Stage2 endpoint from the
        # current observation.
        self.reset()
        self._rlt_segment_active = True
        self._rlt_segment_has_action = False
        self._monitor_status(
            control_mode="RLT",
            rlt_segment_state="ACTIVE",
            stage2_episode_id=self._episode_id,
            stage2_step=self._episode_step,
            last_rlt_ack="PENDING",
            last_event="r pressed: Stage2 segment activated",
        )
        logging.info(
            "RLT segment started. The server may keep executing the frozen VLA "
            "until its configured warmup update gate is ready."
        )
        return True

    def end_rlt_segment(self, *, success: bool) -> bool:
        """Queue a terminal label and return routing to the frozen VLA."""
        self.wait_for_intervention_feedback()
        if not self.cfg.rlt_stage2_transition_feedback:
            return False
        if not self._rlt_segment_active:
            logging.info("RLT segment outcome ignored because Stage2 is not active.")
            return False
        if not self._rlt_segment_has_action:
            logging.warning(
                "RLT segment outcome ignored because no segment action has executed yet."
            )
            return False
        self._pending_segment_success = bool(success)
        self._rlt_segment_active = False
        self.reset()
        self._monitor_status(
            control_mode="VLA",
            rlt_segment_state="TERMINAL PENDING",
            last_rlt_outcome="SUCCESS" if success else "FAILURE",
            last_rlt_ack="PENDING",
            last_event=("s pressed: success label queued" if success else "f pressed: failure label queued"),
        )
        logging.info(
            "RLT segment marked %s; the next observation will close the segment "
            "and resume frozen-VLA routing.",
            "success" if success else "failure",
        )
        return True

    def reset_action_queue(self) -> None:
        """Discard queued policy actions when human control is released."""
        self.reset()

    def publish_monitor_observation(
        self,
        observation_frame: dict[str, np.ndarray],
        *,
        task: str | None = None,
    ) -> None:
        """Publish frames already captured by the robot loop to the monitor."""
        if self._monitor is None:
            return
        self._monitor.publish_observation(observation_frame)
        self._monitor_status(task=(task or "").strip())

    def update_runtime_status(
        self,
        *,
        is_intervention: bool,
        policy_execution_paused: bool,
        selected_from_policy: bool,
    ) -> None:
        """Publish the action selector state without influencing robot control."""
        now = time.perf_counter()
        if self._last_control_tick_t is not None:
            dt_s = now - self._last_control_tick_t
            if dt_s > 0:
                instantaneous_hz = 1.0 / dt_s
                self._control_hz_ema = (
                    instantaneous_hz
                    if self._control_hz_ema is None
                    else 0.9 * self._control_hz_ema + 0.1 * instantaneous_hz
                )
        self._last_control_tick_t = now

        if is_intervention:
            mode = "HUMAN"
            source = "HUMAN TELEOP"
        elif policy_execution_paused:
            mode = "HOLD"
            source = "OBSERVED POSE HOLD"
        elif self._rlt_segment_active:
            mode = "RLT"
            source = "STAGE2 POLICY"
        else:
            mode = "VLA"
            source = "VLA POLICY" if selected_from_policy else "TELEOP FALLBACK"
        self._monitor_status(
            control_mode=mode,
            action_source=source,
            control_hz=self._control_hz_ema,
        )

    def predict_action(
        self,
        *,
        observation_frame: dict[str, np.ndarray],
        action_dim: int,
        task: str | None,
        robot_type: str | None,
    ) -> torch.Tensor:
        self.publish_monitor_observation(observation_frame, task=task)
        request_started_t = time.perf_counter()
        if self._vla_policy is not None:
            if self._pending_segment_success is not None:
                payload = self._build_payload(
                    observation_frame,
                    task=task,
                    robot_type=robot_type,
                )
                self._flush_segment_terminal(
                    payload,
                    success=self._pending_segment_success,
                )
                self._monitor_status(
                    rlt_segment_state="INACTIVE",
                    last_rlt_ack="ACKNOWLEDGED",
                    last_event="Stage2 terminal acknowledged; resumed VLA",
                )
                self._clear_stage2_episode()
            if not self._rlt_segment_active:
                queued_before = len(self._vla_policy._action_queue)
                try:
                    action = self._vla_policy.predict_action(
                        observation_frame=observation_frame,
                        action_dim=action_dim,
                        task=task,
                        robot_type=robot_type,
                    )
                except Exception as err:
                    self._monitor_status(last_error=f"VLA request failed: {err}", vla_connected=False)
                    raise
                self._monitor_status(
                    action_source="VLA QUEUE" if queued_before else "VLA SERVER",
                    inference_latency_ms=(time.perf_counter() - request_started_t) * 1000.0,
                    vla_queue_size=len(self._vla_policy._action_queue),
                    vla_connected=self._vla_policy._ws is not None,
                    stage2_connected=self._ws is not None,
                    last_error=None,
                )
                return action

        if self.cfg.queue_actions and self._action_queue:
            return self._action_to_tensor(self._action_queue.popleft(), action_dim)

        payload = self._build_payload(observation_frame, task=task, robot_type=robot_type)
        if self.cfg.rlt_stage2_transition_feedback:
            if self._episode_id is None:
                raise RuntimeError("Call `start_episode()` before Stage 2 inference.")
            if self._pending_segment_success is not None:
                self._flush_segment_terminal(
                    payload,
                    success=self._pending_segment_success,
                )
                self._begin_protocol_episode()
                payload = self._build_payload(
                    observation_frame,
                    task=task,
                    robot_type=robot_type,
                )
            self._attach_stage2_control(payload)
        response = self._infer(payload)
        if self.cfg.rlt_stage2_transition_feedback:
            requested_segment_active = bool(self._rlt_segment_active)
            self._episode_start_pending = False
            self._pending_executed_action = None
            self._pending_is_intervention = False
            self._pending_transition_in_segment = False
            self._last_observation_frame = dict(observation_frame)
            self._episode_step += 1
            self._last_requested_segment_active = requested_segment_active
            self._rlt_segment_has_action = bool(
                self._rlt_segment_has_action or requested_segment_active
            )
            self._monitor_status(
                control_mode="RLT" if requested_segment_active else "VLA",
                rlt_segment_state="ACTIVE" if requested_segment_active else "INACTIVE",
                action_source="STAGE2 POLICY" if requested_segment_active else "STAGE2 REFERENCE",
                inference_latency_ms=(time.perf_counter() - request_started_t) * 1000.0,
                stage2_connected=self._ws is not None,
                vla_connected=(
                    self._vla_policy is not None and self._vla_policy._ws is not None
                ),
                stage2_episode_id=self._episode_id,
                stage2_step=self._episode_step,
                last_error=None,
            )
        action = self._extract_next_action(response, action_dim)
        return self._action_to_tensor(action, action_dim)

    def _flush_segment_terminal(
        self,
        payload: dict[str, Any],
        *,
        success: bool,
    ) -> None:
        """Close the current Gym episode while the outer robot task continues."""
        if self._pending_executed_action is None:
            raise RuntimeError(
                "Cannot close an RLT segment before its last executed action is recorded."
            )
        self._attach_stage2_control(
            payload,
            reward=1.0 if success else 0.0,
            terminated=True,
            rlt_switch=False,
        )
        response = self._infer(payload)
        episode_done = self._get_response_value(response, "episode_done")
        if episode_done is not True:
            raise RuntimeError(
                f"Stage2 server segment acknowledgement was {episode_done!r}."
            )
        self._monitor_status(last_rlt_ack="ACKNOWLEDGED")

    def _begin_protocol_episode(self) -> None:
        """Start a new remote Gym episode without ending the LeRobot task."""
        self._episode_id = uuid.uuid4().hex
        self._episode_step = 0
        self._episode_start_pending = True
        self._pending_executed_action = None
        self._pending_is_intervention = False
        self._pending_transition_in_segment = False
        self._rlt_segment_active = False
        self._rlt_segment_has_action = False
        self._last_requested_segment_active = False
        self._pending_segment_success = None
        self._last_observation_frame = None
        self.reset()

    def record_execution(
        self,
        *,
        executed_action: dict[str, Any],
        requested_action: dict[str, Any] | None,
        action_feature_names: list[str],
        is_intervention: bool,
    ) -> None:
        """Remember the actual robot command for the next Stage 2 request."""
        if not self.cfg.rlt_stage2_transition_feedback:
            return
        if self._episode_id is None:
            # Ordinary VLA actions are intentionally excluded from the Stage2
            # transition stream.  The deployment dataset is recorded by the
            # existing HIL loop independently of this protocol.
            if self._vla_policy is not None:
                return
            raise RuntimeError("Cannot record a Stage 2 action before `start_episode()`.")

        actual = self._action_dict_to_array(executed_action, action_feature_names)
        safety_clipped = False
        if requested_action is not None:
            requested = self._action_dict_to_array(requested_action, action_feature_names)
            safety_clipped = not np.allclose(
                actual,
                requested,
                rtol=0.0,
                atol=self.cfg.executed_action_tolerance,
            )
        self._pending_executed_action = actual
        human_intervention = bool(is_intervention)
        self._pending_transition_in_segment = bool(
            self._last_requested_segment_active
        )
        # Rate limiting can change ordinary reference actions before the human
        # has identified the critical phase.  Do not let that safety mechanism
        # prematurely latch the Stage2 actor.  Once Stage2 is active, still
        # replace clipped policy actions with the actual executed values so Q
        # learning sees the true transition.
        self._pending_is_intervention = bool(
            human_intervention
            or (safety_clipped and self._pending_transition_in_segment)
        )
        if safety_clipped:
            logging.info(
                "Robot safety clipping changed the requested action "
                "(stage2_active=%s, record_actual=%s).",
                self._pending_transition_in_segment,
                self._pending_is_intervention,
            )

    def finish_episode(
        self,
        *,
        observation_frame: dict[str, np.ndarray],
        action_dim: int,
        task: str | None,
        robot_type: str | None,
        success: bool,
        truncated: bool,
    ) -> None:
        """Flush the final transition and its outcome to the Stage 2 learner."""
        self.wait_for_intervention_feedback()
        if self._vla_policy is not None:
            self._vla_policy.reset()
        if not self.cfg.rlt_stage2_transition_feedback or self._episode_id is None:
            return
        if self._pending_segment_success is not None:
            payload = self._build_payload(
                observation_frame,
                task=task,
                robot_type=robot_type,
            )
            self._flush_segment_terminal(
                payload,
                success=self._pending_segment_success,
            )
            self._clear_stage2_episode()
            return
        if self._last_observation_frame is None:
            # No request reached the server, so no remote episode exists.
            self._clear_stage2_episode()
            return

        payload = self._build_payload(observation_frame, task=task, robot_type=robot_type)
        self._attach_stage2_control(
            payload,
            reward=1.0 if success else 0.0,
            terminated=not truncated,
            truncated=truncated,
        )
        response = self._infer(payload)
        episode_done = self._get_response_value(response, "episode_done")
        if episode_done is not True:
            logging.warning("Stage 2 server terminal acknowledgement was %r.", episode_done)
        self._clear_stage2_episode()

    def _attach_stage2_control(
        self,
        payload: dict[str, Any],
        *,
        reward: float = 0.0,
        terminated: bool = False,
        truncated: bool = False,
        rlt_switch: bool | None = None,
    ) -> None:
        if rlt_switch is None:
            rlt_switch = self._rlt_segment_active
        payload["_rlt_stage2"] = {
            "episode_id": self._episode_id,
            "step_id": self._episode_step,
            "episode_start": self._episode_start_pending,
            "executed_action": self._pending_executed_action,
            "reward": float(reward),
            "terminated": bool(terminated),
            "truncated": bool(truncated),
            "is_intervention": self._pending_is_intervention,
            "rlt_switch": bool(rlt_switch),
        }

    def _clear_stage2_episode(self) -> None:
        self._episode_id = None
        self._episode_step = 0
        self._episode_start_pending = False
        self._pending_executed_action = None
        self._pending_is_intervention = False
        self._pending_transition_in_segment = False
        self._rlt_segment_active = False
        self._rlt_segment_has_action = False
        self._last_requested_segment_active = False
        self._pending_segment_success = None
        self._last_observation_frame = None
        self.reset()

    def _monitor_status(self, **updates: Any) -> None:
        if self._monitor is not None:
            self._monitor.update_status(**updates)

    @staticmethod
    def _action_dict_to_array(
        action: dict[str, Any], action_feature_names: list[str]
    ) -> np.ndarray:
        missing = [name for name in action_feature_names if name not in action]
        if missing:
            raise KeyError(f"Executed robot action is missing features: {missing}")
        array = np.asarray(
            [action[name] for name in action_feature_names], dtype=np.float32
        ).reshape(-1)
        if not np.all(np.isfinite(array)):
            raise ValueError("Robot reported a non-finite executed action.")
        return array

    def _infer(self, payload: dict[str, Any]) -> Any:
        ws = self._ensure_connected()
        try:
            ws.send(_packb(payload))
            response = ws.recv(timeout=self.cfg.timeout_s)
            if isinstance(response, str):
                raise RuntimeError(f"Error in OpenPI websocket inference server:\n{response}")
            return _unpackb(response)
        except RuntimeError:
            self._close()
            raise
        except Exception as err:
            self._close()
            raise ConnectionError(f"Remote OpenPI websocket request failed: {err}") from err

    def _ensure_connected(self) -> Any:
        if self._ws is not None:
            return self._ws

        try:
            import websockets.sync.client
        except ImportError as err:
            raise ImportError(
                "Remote OpenPI websocket inference requires the `websockets` package. "
                "Install it with `pip install websockets`."
            ) from err

        uri = self._normalize_ws_url(self.cfg.url)
        headers = {"Authorization": f"Api-Key {self.cfg.api_key}"} if self.cfg.api_key else None
        logging.info("Connecting to OpenPI websocket policy server at %s.", uri)
        try:
            try:
                self._ws = websockets.sync.client.connect(
                    uri,
                    compression=None,
                    max_size=None,
                    open_timeout=self.cfg.timeout_s,
                    additional_headers=headers,
                )
            except TypeError:
                self._ws = websockets.sync.client.connect(
                    uri,
                    compression=None,
                    max_size=None,
                    open_timeout=self.cfg.timeout_s,
                    extra_headers=headers,
                )
            metadata = self._ws.recv(timeout=self.cfg.timeout_s)
            if isinstance(metadata, str):
                raise RuntimeError(f"OpenPI websocket server returned metadata error:\n{metadata}")
            self._server_metadata = _unpackb(metadata)
            logging.info("Connected to OpenPI websocket policy server. metadata=%s", self._server_metadata)
            return self._ws
        except Exception:
            self._close()
            raise

    def _close(self) -> None:
        if self._ws is None:
            return
        try:
            self._ws.close()
        except Exception:
            logging.debug("Failed to close OpenPI websocket connection.", exc_info=True)
        finally:
            self._ws = None

    @staticmethod
    def _normalize_ws_url(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme:
            return f"ws://{url}"
        if parsed.scheme in {"ws", "wss"}:
            return url
        if parsed.scheme in {"http", "https"}:
            scheme = "wss" if parsed.scheme == "https" else "ws"
            path = "" if parsed.path == "/infer" else parsed.path
            return urlunparse((scheme, parsed.netloc, path, "", parsed.query, ""))
        raise ValueError(f"Unsupported remote policy URL scheme: {parsed.scheme}")

    def _build_payload(
        self,
        observation_frame: dict[str, np.ndarray],
        *,
        task: str | None,
        robot_type: str | None,
    ) -> dict[str, Any]:
        # Build the effective prompt: base task + optional adv_token.
        # adv_token is only sent to the inference server; it is NOT saved in the dataset.
        effective_task = (task or "").strip()
        if self.cfg.adv_token:
            adv = self.cfg.adv_token.strip()
            if adv:
                effective_task = f"{effective_task} {adv}".strip() if effective_task else adv

        if self.cfg.request_format == "pi05":
            payload = self._build_pi05_payload(observation_frame, task=effective_task)
            payload.update(self.cfg.extra_payload)
            return payload

        if self.cfg.request_format == "lerobot":
            payload = dict(observation_frame)
        else:
            payload = self._build_openpi_payload(observation_frame)

        if self.cfg.include_lerobot_keys:
            payload.update(observation_frame)
        if self.cfg.prompt_key:
            payload[self.cfg.prompt_key] = effective_task
        if self.cfg.include_task_key:
            payload["task"] = effective_task
        if self.cfg.include_robot_type_key:
            payload["robot_type"] = robot_type or ""
        payload.update(self.cfg.extra_payload)
        return payload

    def _build_pi05_payload(
        self,
        observation_frame: dict[str, np.ndarray],
        *,
        task: str | None,
    ) -> dict[str, Any]:
        if self.cfg.state_source_key not in observation_frame:
            raise RuntimeError(
                f"Remote pi05 payload requires state key '{self.cfg.state_source_key}'. "
                f"Got keys: {list(observation_frame)}"
            )

        images: dict[str, np.ndarray] = {}
        for key, value in observation_frame.items():
            if not key.startswith(f"{OBS_IMAGES}."):
                continue
            target_key = self._pi05_camera_key(key)
            if target_key is None:
                continue
            image = self._normalize_image_hwc(value)
            images[target_key] = self._image_hwc_to_chw(self._resize_with_pad(image, 224, 224))

        required_images = ("cam_high", "cam_left_wrist", "cam_right_wrist")
        missing = [key for key in required_images if key not in images]
        if missing:
            available = [key for key in observation_frame if key.startswith(f"{OBS_IMAGES}.")]
            raise RuntimeError(
                f"Remote pi05 payload is missing images {missing}. "
                f"Available image keys: {available}"
            )

        state = np.asarray(observation_frame[self.cfg.state_source_key], dtype=np.float64).reshape(-1)
        return {
            "images": {key: images[key] for key in required_images},
            "state": state,
            "prompt": task or "",
        }

    @staticmethod
    def _pi05_camera_key(source_key: str) -> str | None:
        camera_name = source_key.removeprefix(f"{OBS_IMAGES}.")
        normalized = camera_name.lower().replace(".", "_").replace("-", "_")

        high_names = {
            "cam_high",
            "high",
            "main",
            "top",
            "front",
            "third",
            "third_person",
            "third_person_camera",
        }
        if normalized in high_names:
            return "cam_high"
        if normalized in {"cam_left_wrist", "left_wrist", "left_hand", "left"}:
            return "cam_left_wrist"
        if normalized in {"cam_right_wrist", "right_wrist", "right_hand", "right"}:
            return "cam_right_wrist"
        if "right" in normalized and ("wrist" in normalized or "hand" in normalized):
            return "cam_right_wrist"
        if "left" in normalized and ("wrist" in normalized or "hand" in normalized):
            return "cam_left_wrist"
        return None

    def _build_openpi_payload(self, observation_frame: dict[str, np.ndarray]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.cfg.state_source_key in observation_frame:
            state = observation_frame[self.cfg.state_source_key]
            payload[self.cfg.state_target_key] = state
            if self.cfg.include_top_level_state:
                payload["state"] = state

        image_targets_used: set[str] = set()
        top_level_images: dict[str, np.ndarray] = {}
        for key, value in observation_frame.items():
            if not key.startswith(f"{OBS_IMAGES}."):
                continue
            image = self._normalize_image_hwc(value)
            top_level_image = self._image_hwc_to_chw(image) if self.cfg.top_level_image_format == "franka" else image
            target_key = self.cfg.image_key_map.get(key)
            if target_key is None:
                target_key = self._default_openpi_image_key(key, image_targets_used)
            payload[target_key] = image
            image_targets_used.add(target_key)
            if self.cfg.include_top_level_images:
                top_level_images.update(
                    self._default_top_level_image_aliases_for_format(key, top_level_image)
                )

        if top_level_images:
            if self.cfg.top_level_image_format == "franka":
                top_level_images = self._complete_franka_images(top_level_images)
            payload["images"] = top_level_images

        return payload

    @staticmethod
    def _normalize_image_hwc(image: Any) -> np.ndarray:
        """Convert a single camera frame to contiguous uint8 RGB in HWC layout."""
        if isinstance(image, torch.Tensor):
            image = image.detach().cpu().numpy()

        array = np.asarray(image)
        while array.ndim > 4 and array.shape[0] == 1:
            array = array[0]

        if array.ndim == 4:
            if array.shape[0] == 1:
                array = array[0]
            elif array.shape[-1] in (1, 3, 4):
                array = array[0]
            elif array.shape[1] in (1, 3, 4):
                array = np.moveaxis(array[0], 0, -1)
            else:
                raise ValueError(f"Unsupported batched image shape for remote policy: {array.shape}")

        if array.ndim == 2:
            array = array[..., None]
        elif array.ndim == 3:
            if array.shape[-1] in (1, 3, 4):
                pass
            elif array.shape[0] in (1, 3, 4):
                array = np.moveaxis(array, 0, -1)
            elif array.shape[1] in (1, 3, 4):
                array = np.moveaxis(array, 1, -1)
            else:
                raise ValueError(f"Unsupported image shape for remote policy: {array.shape}")
        else:
            raise ValueError(f"Unsupported image rank for remote policy: shape={array.shape}")

        if array.shape[-1] == 1:
            array = np.repeat(array, 3, axis=-1)
        elif array.shape[-1] == 4:
            array = array[..., :3]
        elif array.shape[-1] != 3:
            raise ValueError(f"Expected RGB image for remote policy after conversion, got shape={array.shape}")

        if array.dtype == np.uint8:
            converted = array
        elif np.issubdtype(array.dtype, np.floating):
            converted = array * 255.0 if array.size and np.nanmax(array) <= 1.0 else array
            converted = np.clip(converted, 0, 255).astype(np.uint8)
        elif np.issubdtype(array.dtype, np.bool_):
            converted = array.astype(np.uint8) * 255
        elif np.issubdtype(array.dtype, np.integer):
            converted = np.clip(array, 0, 255).astype(np.uint8)
        else:
            raise ValueError(f"Unsupported image dtype for remote policy: {array.dtype}")

        return np.ascontiguousarray(converted)

    @staticmethod
    def _image_hwc_to_chw(image: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(np.moveaxis(image, -1, 0))

    @staticmethod
    def _resize_with_pad(image: np.ndarray, target_h: int, target_w: int) -> np.ndarray:
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError(f"Expected RGB image HxWx3, got shape {image.shape}.")

        h, w = image.shape[:2]
        if h <= 0 or w <= 0:
            raise ValueError(f"Invalid image shape: {image.shape}.")

        scale = min(target_w / w, target_h / h)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        resized = OpenPIWebsocketRemotePolicy._resize_nearest(image, new_h, new_w)

        canvas = np.zeros((target_h, target_w, 3), dtype=resized.dtype)
        y0 = (target_h - new_h) // 2
        x0 = (target_w - new_w) // 2
        canvas[y0 : y0 + new_h, x0 : x0 + new_w] = resized
        return canvas

    @staticmethod
    def _resize_nearest(image: np.ndarray, new_h: int, new_w: int) -> np.ndarray:
        h, w = image.shape[:2]
        y_idx = np.clip(np.round(np.linspace(0, h - 1, new_h)).astype(np.int64), 0, h - 1)
        x_idx = np.clip(np.round(np.linspace(0, w - 1, new_w)).astype(np.int64), 0, w - 1)
        return image[y_idx][:, x_idx]

    @staticmethod
    def _default_openpi_image_key(source_key: str, used_targets: set[str]) -> str:
        camera_name = source_key.removeprefix(f"{OBS_IMAGES}.")
        normalized = camera_name.lower().replace(".", "_").replace("-", "_")

        if any(token in normalized for token in ("wrist", "hand")) and "observation/wrist_image" not in used_targets:
            return "observation/wrist_image"
        if (
            any(token in normalized for token in ("front", "main", "agent", "rgb", "image"))
            and "observation/image" not in used_targets
        ):
            return "observation/image"
        if "observation/image" not in used_targets:
            return "observation/image"
        return f"observation/{normalized}_image"

    @staticmethod
    def _default_top_level_image_aliases(source_key: str, image: np.ndarray) -> dict[str, np.ndarray]:
        camera_name = source_key.removeprefix(f"{OBS_IMAGES}.")
        normalized = camera_name.lower().replace(".", "_").replace("-", "_")
        aliases: dict[str, np.ndarray] = {}

        if any(token in normalized for token in ("wrist", "hand")):
            aliases["left_wrist_0_rgb"] = image
            aliases["right_wrist_0_rgb"] = image
            aliases["cam_left_wrist"] = image
            aliases["cam_right_wrist"] = image
            aliases["wrist_image"] = image
        else:
            aliases["base_0_rgb"] = image
            aliases["cam_high"] = image
            aliases["image"] = image
            aliases[normalized] = image

        return aliases

    def _default_top_level_image_aliases_for_format(
        self, source_key: str, image: np.ndarray
    ) -> dict[str, np.ndarray]:
        if self.cfg.top_level_image_format == "aliases":
            return self._default_top_level_image_aliases(source_key, image)

        camera_name = source_key.removeprefix(f"{OBS_IMAGES}.")
        normalized = camera_name.lower().replace(".", "_").replace("-", "_")
        if any(token in normalized for token in ("wrist", "hand", "right")):
            return {"cam_left_wrist": image, "cam_right_wrist": image}
        if "low" in normalized:
            return {"cam_low": image}
        return {"cam_high": image}

    @staticmethod
    def _complete_franka_images(images: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        if "cam_high" not in images:
            first_image = next(iter(images.values()))
            images["cam_high"] = first_image
        if "cam_low" not in images:
            images["cam_low"] = images["cam_high"]
        if "cam_left_wrist" not in images:
            images["cam_left_wrist"] = images["cam_high"]
        if "cam_right_wrist" not in images:
            images["cam_right_wrist"] = images["cam_left_wrist"]
        return {
            "cam_high": images["cam_high"],
            "cam_low": images["cam_low"],
            "cam_left_wrist": images["cam_left_wrist"],
            "cam_right_wrist": images["cam_right_wrist"],
        }

    def _extract_next_action(self, response: Any, action_dim: int) -> np.ndarray:
        actions = self._get_response_value(response, self.cfg.action_key)
        if actions is None:
            raise RuntimeError(
                f"Remote policy response did not contain action key '{self.cfg.action_key}'. "
                f"Got keys: {list(response) if isinstance(response, dict) else type(response)}"
            )

        actions_np = np.asarray(actions, dtype=np.float32)
        if actions_np.ndim == 3 and actions_np.shape[0] == 1:
            actions_np = actions_np[0]

        if actions_np.ndim == 1:
            if self.cfg.request_format == "pi05" and actions_np.shape[0] >= 14:
                actions_np = actions_np.copy()
                actions_np[[6, 13]] = np.where(actions_np[[6, 13]] < 1e-2, 0.0, actions_np[[6, 13]])
            return self._apply_action_bounds(actions_np)

        if actions_np.ndim != 2:
            raise RuntimeError(f"Remote policy returned unsupported action shape {actions_np.shape}.")

        if self.cfg.request_format == "pi05" and actions_np.shape[1] >= 14:
            actions_np = actions_np.copy()
            actions_np[:, [6, 13]] = np.where(actions_np[:, [6, 13]] < 1e-2, 0.0, actions_np[:, [6, 13]])
        actions_np = self._apply_action_bounds(actions_np)

        action_index = min(self.cfg.action_index, actions_np.shape[0] - 1)
        first_action = actions_np[action_index]

        if self.cfg.queue_actions:
            chunk = actions_np[action_index + 1 :]
            if self.cfg.max_queued_actions is not None:
                chunk = chunk[: self.cfg.max_queued_actions]
            self._action_queue.extend(np.asarray(action, dtype=np.float32) for action in chunk)
            logging.debug("Queued %d remote policy actions.", len(chunk))

        return first_action

    def _apply_action_bounds(self, actions: np.ndarray) -> np.ndarray:
        """Clamp server actions in policy units before they reach ROS."""
        if self.cfg.action_low is None or self.cfg.action_high is None:
            return actions
        low = np.asarray(self.cfg.action_low, dtype=np.float32).reshape(-1)
        high = np.asarray(self.cfg.action_high, dtype=np.float32).reshape(-1)
        if actions.shape[-1] != low.size:
            raise RuntimeError(
                "Remote policy action dimension does not match configured bounds: "
                f"{actions.shape[-1]} != {low.size}."
            )
        bounded = np.clip(actions, low, high)
        if not np.array_equal(bounded, actions):
            logging.warning(
                "Remote policy action was clipped to the configured absolute safety envelope."
            )
        return bounded

    @staticmethod
    def _get_response_value(response: Any, key: str) -> Any:
        if not isinstance(response, dict):
            return None
        if key in response:
            return response[key]
        key_bytes = key.encode("utf-8")
        return response.get(key_bytes)

    @staticmethod
    def _action_to_tensor(action: np.ndarray, action_dim: int) -> torch.Tensor:
        action = np.asarray(action, dtype=np.float32)
        if action.ndim != 1:
            raise RuntimeError(f"Expected one action vector, got shape {action.shape}.")
        if action.shape[0] < action_dim:
            raise RuntimeError(
                f"Remote policy returned action_dim={action.shape[0]}, but robot expects {action_dim}."
            )
        return torch.from_numpy(action[:action_dim].copy()).unsqueeze(0)


# Backward-compatible name for older imports in this branch.
OpenPIHTTPRemotePolicy = OpenPIWebsocketRemotePolicy

"""Non-blocking policy orchestration shared by all three runner locations."""

from __future__ import annotations

import base64
import logging
import ssl
import threading
import time

import numpy as np

from .control import vector

logger = logging.getLogger(__name__)


class PolicyRunner:
    def __init__(self, cfg, location, emit):
        self.cfg, self.location, self.emit = cfg, location, emit
        self.lock = threading.Lock()
        self.latest = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"policy-{location}")
        self.error = None
        self.error_detail = None
        self.client = None

    def start(self):
        self.thread.start()

    def update(self, observation, *, observed_at=None):
        # Only the in-process robot producer supplies a comparable monotonic
        # capture time. Never compare clocks belonging to different hosts.
        received = time.monotonic()
        with self.lock:
            self.latest = (observation, received, observed_at)

    def _allowed(self, obs):
        status = obs.get("status", {})
        return status.get("runner_location") == self.location and (
            status.get("mode") == "POLICY_ACTIVE"
            or status.get("mode") == "RESUME_PENDING" and status.get("leader_ready")
        )

    def _still_current(self, epoch):
        with self.lock:
            latest = self.latest
        return (
            not self.stop_event.is_set() and latest is not None
            and latest[0].get("status", {}).get("epoch") == epoch
            and self._allowed(latest[0])
        )

    def _create_client(self):
        import os

        from remote_teleop.components.remote_policy import OpenPIWebsocketRemotePolicy, RemotePolicyInferenceConfig

        outer = self.cfg

        class BoundedPolicy(OpenPIWebsocketRemotePolicy):
            def _ensure_connected(self):
                if self._ws is not None:
                    return self._ws
                from websockets.sync.client import connect
                from remote_teleop.components.remote_policy.openpi_http import _unpackb

                uri = self._normalize_ws_url(self.cfg.url)
                kwargs = {}
                if uri.startswith("wss:"):
                    kwargs["ssl"] = ssl.create_default_context(cafile=outer.policy.ca_file or None)
                headers = {"Authorization": f"Api-Key {self.cfg.api_key}"} if self.cfg.api_key else None
                self._ws = connect(
                    uri,
                    additional_headers=headers,
                    compression=None,
                    max_size=outer.network.max_message_bytes,
                    open_timeout=self.cfg.timeout_s,
                    **kwargs,
                )
                metadata = self._ws.recv(timeout=self.cfg.timeout_s)
                if isinstance(metadata, str):
                    self._close()
                    raise RuntimeError("POLICY_HANDSHAKE_FAILED")
                self._server_metadata = _unpackb(metadata)
                return self._ws

        return BoundedPolicy(
            RemotePolicyInferenceConfig(
                enable=True,
                url=self.cfg.policy.url,
                request_format=self.cfg.policy.request_format,
                timeout_s=self.cfg.policy.request_timeout_seconds,
                max_queued_actions=self.cfg.policy.max_queued_actions,
                api_key=os.getenv(self.cfg.policy.api_key_env),
                policy_id=self.cfg.policy.policy_id,
            )
        )

    def _run(self):
        epoch, seq, chunk_observation, chunk_started = None, 0, None, 0.0
        while not self.stop_event.is_set():
            started = time.monotonic()
            with self.lock:
                latest = self.latest
            if latest is None:
                self.stop_event.wait(0.02)
                continue
            obs, received, observed_at = latest
            status = obs.get("status", {})
            if not self._allowed(obs):
                self.stop_event.wait(0.02)
                continue
            if (
                self.cfg.safety.checks.observation_freshness
                and started - received > self.cfg.safety.observation_max_age_ms / 1000
            ):
                self.stop_event.wait(0.02)
                continue
            requested = False
            try:
                if status["epoch"] != epoch:
                    epoch, seq = status["epoch"], 0
                    if self.client:
                        self.client.reset_action_queue()
                if self.cfg.policy.backend == "sim":
                    action = vector(obs["q"])
                    action[0] = 0.15 * np.sin(started / 3)
                    action[7] = -action[0]
                    chunk_observation = obs["observation_id"]
                else:
                    if self.client is None:
                        self.client = self._create_client()
                    queued = bool(self.client._action_queue)
                    lifetime = max(0, self.cfg.policy.max_chunk_age_ms / 1000 - 1 / self.cfg.policy.action_hz)
                    if (
                        queued
                        and self.cfg.safety.checks.command_freshness
                        and started - chunk_started > lifetime
                    ):
                        self.client.reset_action_queue()
                        queued = False
                    frame = {}
                    if not queued:
                        requested = True
                        # Capture age includes image encoding and inference. For
                        # remote runners reserve an age budget instead of making
                        # an observation younger when it arrives over the wire.
                        reserve = min(self.cfg.safety.observation_max_age_ms,
                                      self.cfg.policy.max_chunk_age_ms / 2) / 1000
                        chunk_observation = obs["observation_id"]
                        chunk_started = observed_at if observed_at is not None else received - reserve
                        import cv2

                        frame = {"observation.state": np.asarray(obs["q"], dtype=np.float32)}
                        for name, encoded in obs["images"].items():
                            image = cv2.imdecode(
                                np.frombuffer(base64.b64decode(encoded, validate=True), dtype=np.uint8),
                                cv2.IMREAD_COLOR,
                            )
                            if image is None:
                                raise ValueError("INVALID_POLICY_IMAGE")
                            frame[f"observation.images.{name}"] = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                        if not self._still_current(epoch):
                            continue
                        self.emit({"type": "policy_request", "epoch": epoch, "seq": seq + 1,
                                   "observation_id": chunk_observation})
                    action = self.client.predict_action(
                        observation_frame=frame,
                        action_dim=14,
                        task=obs.get("task", self.cfg.general.task),
                        robot_type="ros_follower",
                    )
                    action = vector(np.asarray(action).reshape(-1).tolist())
                    # A slow result cannot retain authority after takeover or
                    # shutdown; invalidate its cached tail as well.
                    if not self._still_current(epoch) or (
                        not queued and observed_at is not None
                        and self.cfg.safety.checks.command_freshness
                        and time.monotonic() - chunk_started > self.cfg.policy.max_chunk_age_ms / 1000
                    ):
                        self.client.reset_action_queue()
                        continue
                seq += 1
                self.emit(
                    {
                        "type": "policy_action",
                        "epoch": epoch,
                        "seq": seq,
                        "observation_id": chunk_observation,
                        "action": action,
                    }
                )
                self.error = None
                self.error_detail = None
            except Exception as exc:
                self.error = type(exc).__name__
                response = getattr(exc, "response", None)
                http_status = getattr(response, "status_code", None)
                code = str(exc)
                self.error_detail = {
                    "error_type": self.error,
                    "http_status": http_status if type(http_status) is int else None,
                    "error_code": code if code in {
                        "INVALID_POLICY_IMAGE", "INVALID_DIMENSION", "INVALID_NUMBER", "POLICY_HANDSHAKE_FAILED"
                    } else None,
                }
                logger.warning("Policy request failed (%s, HTTP %s)", self.error, http_status)
                if self._still_current(epoch):
                    self.emit({"type": "policy_error", "epoch": epoch, "seq": seq + 1,
                               "observation_id": chunk_observation if requested else obs["observation_id"],
                               "error": self.error_detail})
                if self.client:
                    self.client.reset_action_queue()
                    self.client._close()
                self.stop_event.wait(0.2)
            period = 1 / self.cfg.policy.action_hz
            # A blocking call has already consumed a full period, but its first
            # result still needs an emission period before a refill may clear it.
            self.stop_event.wait(max(period if requested else 0, period - (time.monotonic() - started)))
        if self.client:
            self.client._close()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=1)

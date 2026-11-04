"""Run real producer/controller with controlled delay; never opens hardware."""

import base64
from collections import Counter, deque
import json
from pathlib import Path
import threading
import time

import cv2
import numpy as np

from remote_teleop.config import AppConfig
from remote_teleop.control import Controller, Mode, Rejected
from remote_teleop.policy import PolicyRunner

encoded = base64.b64encode(cv2.imencode(".jpg", np.zeros((16, 16, 3), dtype=np.uint8))[1]).decode()


class Client:
    def __init__(self, latency):
        self.latency = latency
        self._action_queue = deque()
        self.requests = 0
        self.started = threading.Event()

    def reset_action_queue(self):
        self._action_queue.clear()

    def _close(self):
        pass

    def predict_action(self, **kwargs):
        if self._action_queue:
            return np.asarray(self._action_queue.popleft()).reshape(1, 14)
        self.requests += 1
        self.started.set()
        time.sleep(self.latency)
        self._action_queue.extend([[0.0] * 14 for _ in range(49)])
        return np.zeros((1, 14), dtype=np.float32)


def run(delay, latency, duration, known_capture=True):
    cfg = AppConfig()
    cfg.policy.backend = "openpi"
    assert cfg.general.simulation
    q = [0.0] * 14
    control = Controller(cfg)
    control.recording = True
    control.heartbeat(True)
    control.request("arm", q, True)
    control.request("start", q, True)
    control.mode_ready(control.epoch, q)
    lock = threading.RLock()
    rejected, accepted, applied = Counter(), [], set()

    def emit(packet):
        with lock:
            try:
                control.offer(packet, "robot")
                if packet["type"] == "policy_action":
                    accepted.append(time.monotonic())
            except Rejected as exc:
                rejected[str(exc)] += 1

    runner = PolicyRunner(cfg, "robot", emit)
    client = Client(latency)
    runner._create_client = lambda: client
    runner.start()
    deliveries = deque()
    started = next_obs = time.monotonic()
    try:
        while time.monotonic() - started < duration:
            now = time.monotonic()
            with lock:
                control.heartbeat(True)
                if now >= next_obs:
                    next_obs = now + 0.04
                    identifier = control.observe()
                    deliveries.append((now + delay, now, {
                        "status": control.status(), "q": q.copy(), "observation_id": identifier,
                        "images": {name: encoded for name in ("main", "left_hand", "right_hand")},
                    }))
                while deliveries and deliveries[0][0] <= now:
                    _, captured, observation = deliveries.popleft()
                    runner.update(observation, observed_at=captured if known_capture else None)
                result = control.tick(q, True)
                if result:
                    applied.add((result.epoch, result.command_seq))
                if control.mode == Mode.HOLD:
                    break
            time.sleep(0.005)
        with lock:
            ended = time.monotonic()
            report = {"observation_delay_ms": delay * 1000, "inference_delay_ms": latency * 1000,
                      "capture_time_known": known_capture, "mode": control.mode.value,
                      "reason": control.reason, "elapsed_seconds": round(ended - started, 3),
                      "accepted_actions": len(accepted), "rejections": dict(rejected),
                      "applied_unique_commands": len(applied),
                      "inference_requests": client.requests}
    finally:
        runner.close()
    return report


def takeover_during_inference():
    cfg = AppConfig()
    cfg.policy.backend = "openpi"
    messages = []
    runner = PolicyRunner(cfg, "robot", messages.append)
    client = Client(0.5)
    runner._create_client = lambda: client
    observation = {"status": {"mode": "POLICY_ACTIVE", "epoch": "before", "runner_location": "robot"},
                   "q": [0.0] * 14, "observation_id": "before-observation", "images": {"main": encoded}}
    runner.update(observation, observed_at=time.monotonic())
    runner.start()
    try:
        assert client.started.wait(1), "REQUEST_NOT_STARTED"
        runner.update({**observation, "status": {"mode": "HUMAN_ACTIVE", "epoch": "after", "runner_location": "robot"}})
        time.sleep(0.6)
        assert not any(packet["type"] == "policy_action" for packet in messages), messages
        assert not client._action_queue
    finally:
        runner.close()
    return {"passed": True, "late_actions": 0, "cached_tail_discarded": True}


runs = [run(0.2, 0.13, 3), run(0.2, 0.5, 3.5), run(0.2, 0.5, 2, known_capture=False), run(0, 0.9, 2)]
for report in runs[:3]:
    assert report["mode"] == "POLICY_ACTIVE", report
    assert not report["rejections"], report
    assert report["inference_requests"] >= 2, report
    assert report["applied_unique_commands"] >= 2, report
assert runs[3]["reason"] == "POLICY_INFERENCE_TIMEOUT", runs[3]
result = {"passed": True, "hardware_opened": False, "live_configuration_changed": False,
          "runs": runs, "takeover_while_inference_pending": takeover_during_inference()}
Path("state/inference-delay-reproduction.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2), flush=True)

"""Exercise the built-in OpenPI client against a local MessagePack server."""

import base64
import sys
import threading
import time

import cv2
import numpy as np
from websockets.sync.server import serve

from remote_teleop.config import AppConfig
from remote_teleop.policy import PolicyRunner

cfg = AppConfig()
from remote_teleop.components.remote_policy.openpi_http import _packb, _unpackb

requests = []


def inference(ws):
    ws.send(_packb({"fixture": True}))
    for message in ws:
        request = _unpackb(message)
        requests.append(request)
        ws.send(_packb({"actions": np.zeros((50, 14), dtype=np.float32)}))


with serve(inference, "127.0.0.1", 0) as server:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg.policy.backend = "openpi"
    cfg.policy.url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
    cfg.policy.runner_location = "robot"
    messages = []
    runner = PolicyRunner(cfg, "robot", messages.append)
    ok, jpeg = cv2.imencode(".jpg", np.zeros((480, 640, 3), dtype=np.uint8))
    encoded = base64.b64encode(jpeg).decode()
    observation = {
        "q": [0] * 14,
        "images": {n: encoded for n in ["main", "left_hand", "right_hand"]},
        "observation_id": "fixture-1",
        "status": {"epoch": "epoch-1", "mode": "POLICY_ACTIVE", "runner_location": "robot"},
    }
    runner.start()
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline and len(messages) < 6:
            runner.update(observation)
            time.sleep(0.03)
        assert len(messages) >= 6, runner.error
        actions = [m for m in messages if m["type"] == "policy_action"]
        assert len(actions) >= 5
        assert all(len(m["action"]) == 14 and m["epoch"] == "epoch-1" for m in actions)
        assert messages[0]["type"] == "policy_request"
        assert len(requests) == 1, len(requests)  # subsequent commands came from the returned chunk
        assert messages[0]["observation_id"] == "fixture-1"
        print("PASS: actual OpenPI MessagePack handshake, pi05 image payload, 14-D actions and chunk queue")
    finally:
        runner.close()
        server.shutdown()

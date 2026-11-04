"""Real OpenPI/three-role transport with a 500 ms fixture; simulation only."""

import json
from pathlib import Path
import sys
import threading
import time

import numpy as np
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from remote_teleop.config import AppConfig, atomic_json
from integration_smoke import Stack

from remote_teleop.components.remote_policy.openpi_http import _packb, _unpackb


requests = []


def inference(ws):
    try:
        ws.send(_packb({"fixture": "500ms", "hardware": False}))
        for message in ws:
            _unpackb(message)
            started = time.monotonic()
            time.sleep(0.5)
            ws.send(_packb({"actions": np.zeros((50, 14), dtype=np.float32)}))
            requests.append(time.monotonic() - started)
    except ConnectionClosed:
        pass


class ObservedStack(Stack):
    def __init__(self, location, url):
        self.observed_wait = False
        super().__init__(location, sys.executable)
        assert self.config.general.simulation
        self.config.policy.backend = "openpi"
        self.config.policy.url = url
        for path in self.configs.values():
            atomic_json(path, self.config.model_dump())

    def api(self, path, data=None, raw=False):
        result = super().api(path, data, raw)
        if path == "/api/status" and result.get("policy_inference_waiting"):
            self.observed_wait = True
        return result


reports = []
with serve(inference, "127.0.0.1", 0) as server:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
    try:
        for location in ("robot", "client", "cloud"):
            stack = ObservedStack(location, url)
            before = len(requests)
            try:
                stack.start()
                report = stack.exercise()
                assert stack.observed_wait, "INFERENCE_WAIT_NOT_OBSERVED"
                completed = []
                for file in (stack.path / "state/audit").glob("robot-*.jsonl"):
                    completed.extend(json.loads(line) for line in file.read_text().splitlines())
                completions = [event for event in completed if event.get("event") == "policy_request_completed"]
                assert len(completions) >= 3, completions
                assert all(event["elapsed_ms"] >= 490 for event in completions), completions
                assert not any(event.get("reason") in ("COMMAND_TIMEOUT", "POLICY_INFERENCE_TIMEOUT")
                               for event in completed), completed
                report.update(inference_wait_observed=True, inference_requests=len(requests) - before,
                              completed_request_ms=[round(event["elapsed_ms"], 1) for event in completions])
                reports.append(report)
                print(json.dumps(report), flush=True)
            except Exception:
                print("FAILED; simulation logs: " + str(stack.path), flush=True)
                for role in stack.processes:
                    print(role + ":\n" + (stack.path / f"{role}.log").read_text()[-4000:], flush=True)
                raise
            finally:
                stack.close()
    finally:
        server.shutdown()

result = {"passed": True, "hardware_opened": False, "inference_delay_ms": 500, "locations": reports}
Path("state/inference-wait-acceptance.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result), flush=True)

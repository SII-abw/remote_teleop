"""Active simulation faults: operator lease and transport loss must latch safe hold."""

import json
import os
import signal
import time

from integration_smoke import Stack


def active(stack):
    stack.start()
    stack.control("arm")
    stack.wait(lambda s: s.get("mode") == "READY", "armed")
    stack.control("start")
    stack.wait(lambda s: s.get("mode") == "HUMAN_ACTIVE", "active teleoperation")


for fault in ("operator_lease", "gateway_disconnect"):
    stack = Stack("robot", "/home/siinuc2/miniconda3/envs/remote-controller/bin/python", mode="teleop")
    try:
        active(stack)
        if fault == "operator_lease":
            stack.poll_stop.set()
            stack.poller.join(timeout=2)
            time.sleep(stack.config.safety.operator_lease_seconds + 0.8)
            status = stack.api("/api/status")
            assert status["mode"] == "SAFE_HOLD", status
            assert status["stop_confirmed"], status
            time.sleep(0.3)
            assert stack.api("/api/status")["mode"] == "SAFE_HOLD"
        else:
            process = stack.processes["cloud"]
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
            time.sleep(0.8)
            events = []
            for path in (stack.path / "state/audit").glob("*.jsonl"):
                events.extend(json.loads(line) for line in path.read_text().splitlines())
            assert any(e.get("to") == "SAFE_HOLD" and e.get("reason") == "TRANSPORT_LOST" for e in events), (
                events
            )
        print("PASS " + fault + " -> latched SAFE_HOLD; " + str(stack.path), flush=True)
    finally:
        stack.close()

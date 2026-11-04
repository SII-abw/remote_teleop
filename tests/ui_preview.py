"""Manual browser acceptance fixture; temporary loopback-only simulated stack."""

import json
import os
import signal
import subprocess
import sys
import time

from integration_smoke import ROOT, Stack
from remote_teleop.config import atomic_json

stack = Stack("robot", "/home/siinuc2/miniconda3/envs/remote-controller/bin/python")
# This public fixture credential is restricted to a one-hour loopback simulation.
credentials = json.loads(stack.path.joinpath("credentials.json").read_text())
for entry in credentials["credentials"]:
    if entry["role"] == "ui":
        entry["token"] = "browser-test-simulation-only-credential-20261022"
atomic_json(stack.path / "credentials.json", credentials)
stack.config.ui.port = 18810
stack.config.recording.inspection_roots = [
    str(p / "datasets")
    for p in (ROOT / "state").glob("smoke-hil-*")
    if (p / "datasets/acceptance_episode/meta/info.json").exists()
]
for role, path in stack.configs.items():
    atomic_json(path, stack.config.model_dump())
    stream = (stack.path / f"{role}.log").open("w")
    stack.logs.append(stream)
    python = stack.slave_python if role == "slave" else sys.executable
    stack.processes[role] = subprocess.Popen(
        [python, "-m", "remote_teleop", role, "--config", str(path)],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        stdout=stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
stop = False


def finish(*_):
    global stop
    stop = True


signal.signal(signal.SIGTERM, finish)
signal.signal(signal.SIGINT, finish)
print("Browser fixture: http://127.0.0.1:18810 ; " + str(stack.path), flush=True)
try:
    deadline = time.monotonic() + 900
    while not stop and time.monotonic() < deadline:
        time.sleep(0.5)
finally:
    stack.close()

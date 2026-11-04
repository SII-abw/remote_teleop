"""Explicit, simulation-only three-process acceptance check. Never imports a hardware adapter.

Run from this project: PYTHONPATH=src <evo-python> tests/integration_smoke.py
All output, configs and logs are confined to a fresh state/smoke-* directory.
"""

import argparse
import http.cookiejar
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from remote_teleop.config import AppConfig, atomic_json
from remote_teleop.security import create_credentials


ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Stack:
    def __init__(self, location, slave_python, mode="hil"):
        self.location, self.mode = location, mode
        (ROOT / "state").mkdir(exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix=f"smoke-{mode}-{location}-", dir=ROOT / "state"))
        self.config = AppConfig()
        self.config.general.operation_mode = mode
        self.config.network.gateway_port = free_port()
        self.config.network.gateway_url = f"ws://127.0.0.1:{self.config.network.gateway_port}"
        self.config.ui.port = free_port()
        self.config.recording.root = str(self.path / "datasets")
        self.config.recording.minimum_free_gb = 0.01
        self.config.policy.runner_location = location
        self.config.security.credentials_file = str(self.path / "credentials.json")
        self.config.network.rpc_timeout_seconds = 30
        self.base = f"http://127.0.0.1:{self.config.ui.port}"
        create_credentials(self.config.security.credentials_file, self.config.general.robot_id, 1)
        entries = json.loads(Path(self.config.security.credentials_file).read_text())["credentials"]
        self.ui_token = next(e["token"] for e in entries if e["role"] == "ui")
        self.robot_token = next(e["token"] for e in entries if e["role"] == "robot")
        self.configs = {}
        for role in ("cloud", "master", "slave"):
            path = self.path / f"{role}.json"
            atomic_json(path, self.config.model_dump())
            self.configs[role] = path
        self.processes, self.logs = {}, []
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.slave_python = slave_python
        self.poll_stop = threading.Event()
        self.poller = None
        self.status = {}

    def start(self):
        for role in ("cloud", "slave", "master"):
            stream = (self.path / f"{role}.log").open("w")
            self.logs.append(stream)
            python = self.slave_python if role == "slave" else sys.executable
            self.processes[role] = subprocess.Popen(
                [python, "-m", "remote_teleop", role, "--config", str(self.configs[role])],
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
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                self.api("/api/login", {"token": self.ui_token})
                break
            except (OSError, ValueError):
                time.sleep(0.2)
        else:
            raise AssertionError("UI startup timed out")

        def poll():
            while not self.poll_stop.wait(0.2):
                try:
                    self.status = self.api("/api/status")
                except Exception:
                    pass

        self.poller = threading.Thread(target=poll, daemon=True)
        self.poller.start()
        self.wait(lambda s: s.get("healthy") and not s.get("leader_error") and s.get("connected"), "ready")
        self.wait(
            lambda s: len(s.get("video", {})) == 3 and max(v["age_ms"] for v in s["video"].values()) < 500,
            "WebRTC video",
        )

    def api(self, path, data=None, raw=False):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json", "X-Teleop-Request": "1"},
        )
        try:
            with self.opener.open(request, timeout=35) as response:
                payload = response.read()
                return payload if raw else json.loads(payload)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"HTTP {exc.code}: {exc.read().decode()[:2000]}") from exc

    def wait(self, predicate, label, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.status):
                return self.status
            for role, process in self.processes.items():
                if process.poll() is not None:
                    raise AssertionError(f"{role} exited with {process.returncode}")
            time.sleep(0.1)
        raise AssertionError(f"Timed out waiting for {label}: {self.status}")

    def control(self, operation, data=None):
        return self.api("/api/control/" + operation, data or {})

    def exercise(self):
        # Unauthenticated access and CSRF reject, while a valid cookie grants access.
        for path, data in [("/api/status", None), ("/api/control/arm", b"{}")]:
            request = urllib.request.Request(self.base + path, data=data)
            try:
                urllib.request.urlopen(request)
                raise AssertionError("Unauthenticated API accepted")
            except urllib.error.HTTPError as exc:
                assert exc.code in (401, 403)
        job = self.control("record_start", {"name": "acceptance_episode"})
        self.wait(
            lambda s: (s.get("record_job") or {}).get("id") == job["id"]
            and (s.get("record_job") or {}).get("state") in ("COMPLETE", "FAILED"),
            "recorder initialization",
            90,
        )
        assert self.status["recorder"]["state"] == "RECORDING", self.status
        self.control("arm")
        self.wait(lambda s: s.get("mode") == "READY", "armed")
        self.control("start")
        first_mode = "POLICY_ACTIVE" if self.mode == "hil" else "HUMAN_ACTIVE"
        self.wait(lambda s: s.get("mode") == first_mode, first_mode)
        time.sleep(1.5)
        if self.mode == "hil":
            self.control("toggle")
            self.wait(lambda s: s.get("mode") == "HUMAN_ACTIVE", "human takeover")
        q = self.status["q"].copy()
        q[1] += 0.025
        self.api("/api/simulation/leader", {"q": q})
        self.wait(lambda s: abs(s.get("q", [0, 0])[1] - q[1]) < 0.01, "human motion")
        time.sleep(1)
        if self.mode == "hil":
            self.control("toggle")
            self.wait(lambda s: s.get("mode") == "POLICY_ACTIVE", "policy resume")
            time.sleep(1.2)
        job = self.control("record_finish", {"outcome": "success"})
        self.wait(
            lambda s: (s.get("record_job") or {}).get("id") == job["id"]
            and (s.get("record_job") or {}).get("state") in ("COMPLETE", "FAILED"),
            "dataset save",
            90,
        )
        assert self.status["recorder"]["state"] == "IDLE", self.status
        datasets = self.api("/api/datasets")
        assert len(datasets) == 1, datasets
        report = self.api("/api/datasets/inspect", {"id": datasets[0]["id"], "deep": True})
        assert report["compatible"], report["issues"]
        assert report["info"]["fps"] == self.config.recording.fps
        assert report["video_files"] == 3, report
        jpeg = self.api("/api/datasets/preview?id=0:acceptance_episode&camera=main", raw=True)
        assert jpeg[:2] == b"\xff\xd8"
        import pyarrow.parquet as pq

        dataset = self.path / "datasets/acceptance_episode"
        rows = []
        for file in (dataset / "data").glob("chunk-*/*.parquet"):
            rows.extend(pq.read_table(file).to_pylist())
        labels = [r["complementary_info.state"] for r in rows]
        assert labels[0] == 0 if self.mode == "hil" else set(labels) == {0}, labels
        if self.mode == "hil":
            assert 1 in labels and labels.count(2) == 1, {v: labels.count(v) for v in set(labels)}
        assert all(len(r["action"]) == 14 for r in rows)
        for row in rows:
            if row["complementary_info.collector_policy_id"] == "human":
                assert row["complementary_info.policy_action"] == [0] * 14
        episode_file = next((dataset / "meta/episodes").glob("chunk-*/*.parquet"))
        assert pq.read_table(episode_file).to_pylist()[0]["episode_success"] == "success"
        assert not (dataset / "audit/incomplete.json").exists()
        self.control("estop")
        self.wait(lambda s: s.get("mode") == "ESTOP_LATCHED", "latched estop")
        try:
            self.control("start")
            raise AssertionError("Estop bypassed")
        except ValueError:
            pass
        self.control("reset")
        self.wait(lambda s: s.get("mode") == "DISARMED", "reset")
        for role in ("client", "robot", "cloud"):
            current = self.api("/api/config/" + role)
            current["config"]["ui"]["language"] = "en"
            saved = self.api("/api/config/" + role, current)
            assert saved["restart_required"], saved
        # Credential revocation terminates the robot session and exposes lost telemetry.
        credentials = json.loads(Path(self.config.security.credentials_file).read_text())
        credentials["credentials"] = [e for e in credentials["credentials"] if e["role"] != "robot"]
        atomic_json(Path(self.config.security.credentials_file), credentials)
        self.wait(lambda s: s.get("mode") == "OFFLINE", "revocation disconnect")
        return {
            "location": self.location,
            "mode": self.mode,
            "frames": len(rows),
            "states": {str(v): labels.count(v) for v in set(labels)},
            "webrtc": True,
            "dataset_compatible": True,
            "log_directory": str(self.path),
        }

    def close(self):
        self.poll_stop.set()
        if self.poller:
            self.poller.join(timeout=2)
        for process in reversed(list(self.processes.values())):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in self.processes.values():
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for stream in self.logs:
            stream.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--slave-python", default=sys.executable
    )
    parser.add_argument("--locations", nargs="+", default=["robot", "client", "cloud"])
    parser.add_argument("--mode", choices=["hil", "teleop"], default="hil")
    args = parser.parse_args()
    reports = []
    for location in args.locations:
        stack = Stack(location, args.slave_python, args.mode)
        try:
            stack.start()
            result = stack.exercise()
            reports.append(result)
            print(json.dumps(result), flush=True)
        except Exception:
            print("FAILED; logs: " + str(stack.path), flush=True)
            for role in stack.processes:
                print(role + ":\n" + (stack.path / f"{role}.log").read_text()[-6000:], flush=True)
            raise
        finally:
            stack.close()
    atomic_json(ROOT / "state" / f"acceptance-{args.mode}.json", {"passed": reports, "time": time.time()})


if __name__ == "__main__":
    main()

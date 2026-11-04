"""Installed-package acceptance: deny all external source imports; use simulated data only."""

import importlib.abc
import importlib.resources
import json
from pathlib import Path
import sys
import tempfile
import time


class BlockExternalSource(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "lerobot" or fullname.startswith("lerobot."):
            raise ImportError("External source access is forbidden in standalone acceptance")
        return None


sys.meta_path.insert(0, BlockExternalSource())

import numpy as np
from remote_teleop.components.remote_policy.openpi_http import (
    OpenPIWebsocketRemotePolicy,
    RemotePolicyInferenceConfig,
    _packb,
    _unpackb,
)
from remote_teleop.components.teleoperators.sii_arm_leader.config_sii_arm_leader import (
    SiiArmLeaderArmConfig,
    SiiArmLeaderConfig,
)
from remote_teleop.components.teleoperators.sii_arm_leader.sii_arm_leader import SiiArmLeader
from remote_teleop.config import AppConfig
from remote_teleop.control import AppliedAction
from remote_teleop.recording import Recorder


def main():
    # Import and configure serial code without constructing/opening a serial device.
    leader_cfg = SiiArmLeaderConfig(arms={
        side: SiiArmLeaderArmConfig(port=f"/nonexistent/{side}") for side in ("left", "right")
    })
    assert len(leader_cfg.arms) == 2
    assert SiiArmLeader is not None
    policy = OpenPIWebsocketRemotePolicy(RemotePolicyInferenceConfig())
    policy._close()
    array = np.zeros((3, 14), dtype=np.float32)
    assert np.array_equal(_unpackb(_packb({"actions": array}))["actions"], array)
    card = importlib.resources.files("remote_teleop.components.datasets") / "card_template.md"
    assert card.is_file()
    with tempfile.TemporaryDirectory(prefix="standalone-recording-") as directory:
        cfg = AppConfig()
        cfg.recording.root = str(Path(directory) / "datasets")
        cfg.recording.minimum_free_gb = 0.01
        cfg.recording.fps = 10
        shapes = {name: (64, 64, 3) for name in ("main", "left_hand", "right_hand")}
        recorder = Recorder(cfg)
        try:
            recorder.start("fixture", shapes)
            q = [0.0] * 14
            for seq in range(6):
                stamp = time.monotonic()
                frames = {name: (np.zeros(shape, dtype=np.uint8), stamp) for name, shape in shapes.items()}
                applied = AppliedAction(q, q, 0.0, 0.0, "policy-fixture", "epoch", f"obs-{seq}", seq, q)
                assert recorder.submit(q, applied, frames, f"obs-{seq}")
            result = recorder.finish("success")
            assert result["state"] == "IDLE", result
            assert result["frames"] == 6, result
            dataset = Path(cfg.recording.root) / "fixture"
            outcome = json.loads((dataset / "audit/outcome.json").read_text())
            assert outcome["outcome"] == "success"
            assert not (dataset / "audit/incomplete.json").exists()
            assert len(list(dataset.rglob("*.mp4"))) == 3
            assert list(dataset.rglob("*.parquet"))
        finally:
            recorder.close()
    assert not any(name == "lerobot" or name.startswith("lerobot.") for name in sys.modules)
    print("PASS: built-in serial imports, OpenPI codec, dataset resource and 6-frame/3-camera H.264 recording; external source blocked")


if __name__ == "__main__":
    main()

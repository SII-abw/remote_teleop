"""Read-only dependency and built-in component manifest for release reproducibility."""

import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

packages = [
    "fastapi",
    "uvicorn",
    "pydantic",
    "websockets",
    "numpy",
    "aiortc",
    "av",
    "pyarrow",
    "torch",
    "opencv-python",
    "opencv-python-headless",
    "msgpack",
    "datasets",
    "feetech-servo-sdk",
]
versions = {}
for package in packages:
    try:
        versions[package] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        versions[package] = None
source = Path(__file__).resolve().parents[1] / "src/remote_teleop/components"
paths = [
    "remote_policy/openpi_http.py",
    "datasets/lerobot_dataset.py",
    "teleoperators/sii_arm_leader/sii_arm_leader.py",
    "teleoperators/sii_arm_leader/config_sii_arm_leader.py",
]
print(
    json.dumps(
        {
            "python": sys.executable,
            "python_version": platform.python_version(),
            "packages": versions,
            "components_sha256": {p: hashlib.sha256((source / p).read_bytes()).hexdigest() for p in paths},
        },
        indent=2,
    )
)

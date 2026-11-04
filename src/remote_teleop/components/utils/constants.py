"""Shared observation keys and package-local cache locations."""
import os
from pathlib import Path

OBS_STR = "observation"
OBS_ENV_STATE = "observation.environment_state"
OBS_STATE = "observation.state"
OBS_IMAGES = "observation.images"
ACTION = "action"
TELEOPERATORS = "teleoperators"
DATASET_CACHE = Path(os.getenv("TELEOP_CACHE_HOME", "~/.cache/remote_teleop")).expanduser()
CALIBRATION_HOME = Path(os.getenv("TELEOP_CALIBRATION_HOME", str(DATASET_CACHE / "calibration"))).expanduser()

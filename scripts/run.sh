#!/usr/bin/env bash
# No implicit hardware enable. Preserve the existing siinuc environment fallback.
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ROLE="${1:?Usage: scripts/run.sh cloud|master|slave [config.json]}"
case "$ROLE" in cloud|master|slave) ;; *) echo 'Role must be cloud, master or slave' >&2; exit 2 ;; esac
cd "$PROJECT_ROOT"
CONFIG_PATH="${2:-}"
if [[ -z "$CONFIG_PATH" ]]; then
  if [[ -f "configs/local/$ROLE.json" ]]; then
    CONFIG_PATH="configs/local/$ROLE.json"
  else
    CONFIG_PATH="configs/siinuc/$ROLE.json"
  fi
fi
if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Missing configuration: $CONFIG_PATH. See docs/DISTRIBUTED_DEPLOYMENT.md." >&2
  exit 2
fi
if [[ -n "${TELEOP_PYTHON:-}" ]]; then
  PYTHON_BIN="$TELEOP_PYTHON"
elif [[ -x "$PROJECT_ROOT/.venv-$ROLE/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_ROOT/.venv-$ROLE/bin/python"
else
  CONDA_ROOT="${TELEOP_CONDA_ROOT:-/home/siinuc2/miniconda3}"
  case "$ROLE" in
    cloud|master) PYTHON_BIN="$CONDA_ROOT/envs/evo-rl/bin/python" ;;
    slave) PYTHON_BIN="$CONDA_ROOT/envs/remote-controller/bin/python" ;;
  esac
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found: $PYTHON_BIN. Run: bash scripts/install.sh $ROLE" >&2
  exit 2
fi
if [[ -n "${TELEOP_ROS_SETUP:-}" ]]; then
  set +u
  source "$TELEOP_ROS_SETUP"
  set -u
fi
export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export LEROBOT_IMAGE_WRITER_QUEUE_MAXSIZE=180
exec "$PYTHON_BIN" -m remote_teleop "$ROLE" --config "$CONFIG_PATH"

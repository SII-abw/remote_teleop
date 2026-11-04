#!/usr/bin/env bash
# Install only the selected role into a dedicated virtual environment.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
usage() {
  cat <<'EOF'
Usage: bash scripts/install.sh cloud|master|slave [options]
  --python PATH    Python >=3.10 (default: python3)
  --venv PATH      Environment path (default: .venv-ROLE)
  --policy         Add OpenPI client dependencies on the selected runner host
  --serial         Add Feetech dependencies on the master
  --dev            Add development tools
  --help           Show this message
This script installs software only; it does not start services or create credentials.
EOF
}
if [[ "${1:-}" == --help ]]; then usage; exit 0; fi
ROLE="${1:-}"
case "$ROLE" in cloud|master|slave) shift ;; *) usage >&2; exit 2 ;; esac
PYTHON_BIN=python3
VENV="$ROOT/.venv-$ROLE"
EXTRAS="$ROLE"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python|--venv)
      [[ $# -ge 2 && -n "$2" ]] || { echo "Missing value for $1" >&2; exit 2; }
      case "$1" in --python) PYTHON_BIN="$2" ;; --venv) VENV="$2" ;; esac
      shift 2 ;;
    --policy) EXTRAS+=",policy"; shift ;;
    --serial)
      [[ "$ROLE" == master ]] || { echo '--serial requires master' >&2; exit 2; }
      EXTRAS+=",serial"; shift ;;
    --dev) EXTRAS+=",dev"; shift ;;
    --help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python >=3.10 is required")'
if [[ ! -e "$VENV" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
elif [[ ! -f "$VENV/pyvenv.cfg" || ! -x "$VENV/bin/python" ]]; then
  echo "Refusing to use a non-venv directory: $VENV" >&2; exit 2
fi
VENV="$(cd -- "$VENV" && pwd)"
"$VENV/bin/python" -m pip install "$ROOT[$EXTRAS]"
"$VENV/bin/python" -m remote_teleop validate --config "$ROOT/configs/simulation.json"
printf 'Installed %s into %s\n' "$ROLE" "$VENV"
printf 'Next: read docs/DISTRIBUTED_DEPLOYMENT.md and prepare a role configuration.\n'

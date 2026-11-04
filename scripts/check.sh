#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
PYTHON_BIN="${TELEOP_TEST_PYTHON:-python}"
export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON_BIN" -m compileall -q src tests
"$PYTHON_BIN" -m unittest discover -s tests -v
if command -v ruff >/dev/null 2>&1; then
  ruff check .
  ruff format --check .
fi
if command -v node >/dev/null 2>&1; then
  node --check src/remote_teleop/web/app.js
  node --check src/remote_teleop/web/i18n.js
fi

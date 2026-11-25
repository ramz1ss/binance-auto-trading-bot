#!/usr/bin/env bash
# Start the local dashboard at http://127.0.0.1:8765 (pass "paper" or "live"; extra flags go to visualizer.py).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=venv/bin/python; [ -x "$PY" ] || PY=python3
mode="${1:-auto}"; shift || true
exec "$PY" visualizer.py --mode "$mode" --open --tab "$@"

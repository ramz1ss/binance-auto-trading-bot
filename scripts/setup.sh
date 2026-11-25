#!/usr/bin/env bash
# Create a private virtual environment and install the pinned requirements (macOS / Linux).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11 or newer is required (found: $("$PY" -V 2>&1))." >&2; exit 1; }
[ -x venv/bin/python ] || "$PY" -m venv venv
venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
[ -f config.toml ] || cp config.example.toml config.toml
echo "Ready. Paper test: scripts/paper.sh   Dashboard: scripts/visualizer.sh paper"

#!/usr/bin/env bash
# Run the PAPER desk (simulated money, real public market data, no API keys). Ctrl+C to stop.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x venv/bin/python ] || scripts/setup.sh
exec venv/bin/python main.py --mode paper "$@"

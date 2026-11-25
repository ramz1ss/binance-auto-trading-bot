#!/usr/bin/env bash
# Run the LIVE desk under a small supervisor (macOS / Linux). REAL MONEY.
# Requires: [live] enabled = true in config.toml and BINANCE_API_KEY / BINANCE_API_SECRET (environment or .env).
# Exit codes from main.py: 0 stopped, 1 crash (restart), 2 needs your attention (no restart), 3 temporary (retry in 60 s).
set -uo pipefail
cd "$(dirname "$0")/.."
[ -x venv/bin/python ] || scripts/setup.sh
mkdir -p logs state
rm -f state/STOP
while true; do
  [ -f state/TARGET_REACHED ] && { echo "Target reached - not restarting."; exit 0; }
  venv/bin/python main.py --mode live "$@"
  rc=$?
  [ -f state/TARGET_REACHED ] && { echo "Target reached."; exit 0; }
  [ -f state/STOP ] && { echo "Stopped. Open positions keep their stop-loss orders on Binance."; exit 0; }
  case "$rc" in
    0) exit 0 ;;
    2) echo "The desk needs your attention - read the [STOP] message above." >&2; exit 2 ;;
    3) echo "$(date -u +%FT%TZ) temporary problem (code 3) - retrying in 60 s" | tee -a logs/supervisor.log; sleep 60 ;;
    *) echo "$(date -u +%FT%TZ) desk exited with code $rc - restarting in 10 s" | tee -a logs/supervisor.log; sleep 10 ;;
  esac
done

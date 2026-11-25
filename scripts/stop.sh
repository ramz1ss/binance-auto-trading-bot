#!/usr/bin/env bash
# Ask a running desk to stop cleanly (live by default; pass "paper" for the paper desk).
# Stopping does NOT sell anything: open live positions keep their exchange-side stop-loss.
set -euo pipefail
cd "$(dirname "$0")/.."
dir="."; [ "${1:-live}" = "paper" ] && dir="paper"
mkdir -p "$dir/state" && touch "$dir/state/STOP"
echo "Stop requested ($dir/state/STOP). The desk exits within about ${2:-30} seconds."

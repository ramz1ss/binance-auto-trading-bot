"""Prints a plain summary of status.json (live) or paper\\status.json (python status.py paper)."""

import json
import os
import sys

root = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(root, "paper", "status.json") if "paper" in sys.argv[1:] else os.path.join(root, "status.json")
try:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
except FileNotFoundError:
    print("No status file yet. The desk has not run (or has not finished its first check).")
    sys.exit(0)
print(f"Mode:            {d['mode'].upper()}")
print(f"State:           {d['state']}   (last update {d['ts']} UTC)")
print(f"Equity:          {d['equity']:.4f} USDT   (started with {d['start_balance']:.4f}; profit/loss {d['pnl']:+.4f} = {d['pnl_pct']:+.2f}%)")
print(f"Free USDT:       {d['cash']:.4f}")
print(f"Target:          {d['target_equity']:,.0f} USDT  ({d['target_progress_pct']:.4f}% of the way)")
print(f"Trades closed:   {d['trades_closed']}  (wins: {d['wins']})")
if d["open_positions"]:
    for p in d["open_positions"]:
        print(
            f"Open position:   {p['symbol']}  qty {p['qty']}  entry {p['entry']:.8g}  now {p['mark_bid']:.8g} ({p['unrealized_pct']:+.2f}%)  stop {p['stop']:.8g}"
            + ("  [trailing]" if p.get("trailing") else "")
        )
else:
    print("Open position:   none")
if d.get("last_error"):
    print(f"Last error at:   {d['last_error']} (see logs\\desk.log)")

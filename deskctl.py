#!/usr/bin/env python3
"""Manual controls for the desk.
   python deskctl.py close  [--mode live|paper] [--symbol SOLUSDT] [--yes] [--pause-after | --no-pause-after]   sell the open trade(s) now
   python deskctl.py pause  [--mode ...]     no NEW entries (open trades stay managed and keep their stop-loss)
   python deskctl.py resume [--mode ...]     allow new entries again
   python deskctl.py status [--mode ...]
If the desk is running it is asked through state/close_request.json (it answers within a few seconds). If it is NOT running, the trade is closed directly here.
STOP.bat only stops the program (the stop-loss stays on Binance). CLOSE-TRADE is the one that SELLS."""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from desk import config as C
from desk import control as CTL
from desk.util import SingleInstance, redact

WAIT_SEC = 120


def open_positions(cfg):
    """What the desk currently holds, from its own state file (live_state.json / state.json)."""
    p = os.path.join(cfg["_data"], "state", "live_state.json" if cfg["mode"] == "live" else "state.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("positions") or {}
    except Exception:
        return {}


def _show(pos, say):
    for s, p in pos.items():
        say(f"   {s}: {p.get('qty')} coins, bought at {p.get('entry')}, stop-loss at {p.get('stop')}" + (" (trailing)" if p.get("trailing") else ""))


def _print_results(results, say):
    for r in results:
        tag = {"closed": "SOLD", "already_closed": "DONE", "failed": "FAILED", "skipped": "SKIPPED"}.get(
            r.get("status"), r.get("status", "?").upper()
        )
        pl = (
            f"  P&L {r['pnl']:+.4f} USDT ({r['pnl_pct']:+.2f}%)"
            if r.get("pnl") is not None and r.get("status") in ("closed", "already_closed")
            else ""
        )
        say(f"  [{tag}] {r.get('symbol')}: {r.get('detail')}{pl}")


def _make_client(cfg, say):
    """A light connection for the direct close: keys + time sync only (the full pre-flight refuses when the balance is low - exactly when you may need to close)."""
    from desk.binance import BinanceClient
    from desk.keys import load_keys

    key, sec, problem = load_keys(cfg["_root"])
    if problem:
        raise SystemExit(problem)
    c = BinanceClient(key, sec, base=cfg["live"]["base_url"], recv_window=cfg["live"].get("recv_window_ms", 5000))
    c.ping()
    c.sync_time()
    return c


def direct_close(cfg, req, say, client=None):
    """The desk is not running: do the close right here, with the same code the running desk uses."""
    from desk.data import Data
    from desk.logger import Logger
    from desk.risk import Risk

    data = Data(cfg)
    lg = Logger(cfg)
    risk = Risk(cfg, data, lg)
    results = []
    if cfg["mode"] == "live":
        from desk.live import LiveBroker

        broker = LiveBroker(cfg, client or _make_client(cfg, say), data, lg)
        for line in broker.reconcile(risk):
            say("  [SYNC] " + line)
        results = broker.close_all(req.get("symbols"))
        [lg.trade(ev) for ev in broker.drain_events()]
    else:
        from desk.broker import PaperBroker

        broker = PaperBroker(cfg)
        bt = data.book_tickers()
        for s in list(broker.positions):
            if req.get("symbols") and s not in req["symbols"]:
                continue
            bid = float(bt[s]["bidPrice"]) if s in bt else broker.positions[s]["entry"]
            notional, fee, pnl, pct = broker.sell(s, bid, "manual")
            lg.trade(
                {
                    "event": "exit",
                    "symbol": s,
                    "side": "SELL",
                    "qty": 0,
                    "price": bid,
                    "notional": notional,
                    "fee": fee,
                    "pnl": pnl,
                    "pnl_pct": pct,
                    "reason": "manual",
                    "mode": "paper",
                }
            )
            results.append({"symbol": s, "status": "closed", "detail": "sold at the simulated bid", "pnl": pnl, "pnl_pct": pct})
    if not results:
        results = [{"symbol": "-", "status": "skipped", "detail": "no open position"}]
    if req.get("pause_after"):
        CTL.set_pause(cfg, True)
    CTL.finish_request(cfg, req, results, paused=os.path.exists(CTL.pause_path(cfg)))
    lg.save_counters()
    return results


def cmd_close(cfg, a, client, input_fn, say):
    pos = open_positions(cfg)
    if a.symbol:
        pos = {s: p for s, p in pos.items() if s in [x.upper() for x in a.symbol]}
    if not pos:
        say("There is no open trade recorded, so there is nothing to close.\nTo stop the desk from opening a new one, run PAUSE.bat.")
        return 0
    say("Open trade(s):")
    _show(pos, say)
    say("\nCLOSE TRADE sells the coin(s) at market price RIGHT NOW and cancels the stop-loss. (STOP.bat does NOT sell anything.)")
    if not a.yes:
        if input_fn("Type YES (capital letters) and press Enter to sell now, or just press Enter to cancel: ").strip() != "YES":
            say("Cancelled. Nothing was sold.")
            return 1
    pause = a.pause_after
    if pause is None and not a.yes:
        pause = input_fn("Also PAUSE new trades afterwards, so the desk does not open another one at the next scan? (Y/n): ").strip().lower() not in (
            "n",
            "no",
        )
    inst = SingleInstance(os.path.join(cfg["_data"], "state", "desk.lock"))
    os.makedirs(os.path.dirname(inst.path), exist_ok=True)
    if inst.acquire():  # nobody is running the desk: close it here, holding the lock so it cannot start meanwhile
        try:
            say("The desk is not running - closing directly...")
            res = direct_close(
                cfg,
                {"source": "CLOSE-TRADE.bat (direct)", "symbols": [x.upper() for x in a.symbol] if a.symbol else None, "pause_after": bool(pause)},
                say,
                client,
            )
        except SystemExit:
            raise
        except Exception as e:
            say(
                f"Could not close directly: {redact(e)}\nSell it by hand in the Binance app: Orders > Open Orders > cancel the stop-loss, then sell the coin."
            )
            return 2
        finally:
            inst.release()
        _print_results(res, say)
        if pause:
            say("New trades are PAUSED. Run RESUME.bat when you want the desk to trade again.")
        return 0 if all(r["status"] != "failed" for r in res) else 2
    req = CTL.write_request(cfg, "CLOSE-TRADE.bat", a.symbol, bool(pause))
    t0 = req["ts_epoch"]
    say(f"The desk is running. Request sent - waiting for it to sell (up to {WAIT_SEC} seconds)...")
    for _ in range(WAIT_SEC):
        time.sleep(1 if not os.environ.get("DESKCTL_FAST") else 0.05)
        try:
            with open(CTL.result_path(cfg), encoding="utf-8") as f:
                r = json.load(f)
            if r.get("ts_epoch", 0) >= t0:
                _print_results(r["results"], say)
                if r.get("paused"):
                    say("New trades are PAUSED. Run RESUME.bat when you want the desk to trade again.")
                return 0 if all(x["status"] != "failed" for x in r["results"]) else 2
        except Exception:
            pass
    try:
        os.remove(CTL.close_path(cfg))
    except OSError:
        pass
    say(
        "The desk did not answer. The request was withdrawn so nothing happens later by surprise.\nIf the desk window is frozen: run STOP.bat, then CLOSE-TRADE.bat again (it then closes directly).\nLast resort: Binance app > Orders > cancel the stop-loss, then sell."
    )
    return 2


def main(argv=None, client=None, input_fn=input, say=print):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["close", "pause", "resume", "status"])
    ap.add_argument("--mode", choices=["live", "paper"], default="live")
    ap.add_argument("--symbol", action="append")
    ap.add_argument("--yes", action="store_true")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--pause-after", dest="pause_after", action="store_true", default=None)
    g.add_argument("--no-pause-after", dest="pause_after", action="store_false")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    cfg = C.load(a.config, a.mode)
    if a.cmd == "pause":
        CTL.set_pause(cfg, True)
        say(
            "PAUSED: the desk will not open new trades. Open trades keep their stop-loss and are still managed.\nRun RESUME.bat to allow new trades again."
        )
        return 0
    if a.cmd == "resume":
        CTL.set_pause(cfg, False)
        say("RESUMED: the desk may open new trades again at its next scan.")
        return 0
    if a.cmd == "status":
        say("Paused: " + ("YES (no new trades)" if os.path.exists(CTL.pause_path(cfg)) else "no"))
        pos = open_positions(cfg)
        say("Open trade(s): " + ("none" if not pos else ""))
        _show(pos, say)
        return 0
    return cmd_close(cfg, a, client, input_fn, say)


if __name__ == "__main__":
    sys.exit(main())

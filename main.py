#!/usr/bin/env python3
"""Autonomous crypto trading desk. Pipeline: Watcher -> Filter -> Analyst -> Executor -> Risk -> Logger.
   python main.py --mode paper   (simulated money, files in .\\paper)
   python main.py --mode live    (REAL Binance Spot orders; runs the pre-flight check first)
Exit codes: 0 = finished/stopped normally, 2 = needs the user to fix something (do not auto-restart), 3 = temporary problem (retry)."""

import argparse
import os
import signal
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from desk import config as C
from desk import control as CTL
from desk.analyst import Analyst
from desk.broker import PaperBroker
from desk.data import Data
from desk.executor import Executor
from desk.filter import Filter
from desk.logger import Logger
from desk.risk import Risk
from desk.util import SingleInstance, iso, redact
from desk.watcher import Watcher

STOP = False


def _sig(*_):
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _sig)  # Windows: closing the console window


def main(argv=None, client=None):
    global STOP
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper", "live"], help="overrides mode in config.toml")
    ap.add_argument("--once", action="store_true", help="run a single scan cycle and exit")
    ap.add_argument("--preflight-only", action="store_true", help="live: run the pre-flight check and exit")
    ap.add_argument("--yes", action="store_true", help="live: skip the first-run 'type YES' confirmation")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = C.load(args.config, args.mode)
    live = cfg["mode"] == "live"
    data_dir = cfg["_data"]
    state_dir = os.path.join(data_dir, "state")
    inst = SingleInstance(os.path.join(state_dir, "desk.lock"))
    if not inst.acquire():
        print("The desk is already running (another START.bat window?). Close it or run STOP.bat first.")
        return 2
    pidfile = os.path.join(state_dir, "desk.pid")
    flag = os.path.join(state_dir, "TARGET_REACHED")
    stopfile = os.path.join(state_dir, "STOP")
    try:
        if live and not C.live_enabled(cfg):
            print("[STOP] Live trading is disabled. This is the safe default.")
            print("       To trade real money you must opt in explicitly: set  [live] enabled = true  in config.toml,")
            print("       provide BINANCE_API_KEY / BINANCE_API_SECRET (environment variables or .env), then run with --mode live.")
            print("       Read docs/paper-vs-live.md first. Nothing was traded.")
            return 2
        if live:
            from desk import preflight

            pf = preflight.run(cfg, client=client)
            if not pf.ok:
                print("=== Pre-flight FAILED. Nothing was traded. ===")
                return 2 if pf.kind == "permanent" else 3
            if args.preflight_only:
                return 0
            confirmed = os.path.join(state_dir, "live_confirmed")
            if not os.path.exists(confirmed) and not args.yes:
                print("\nThis will trade REAL money on your Binance account (one position at a time, -8% stop, trailing stop after +10%).")
                print("You can lose part or all of the money. Nobody can promise profit.")
                try:
                    ans = input("Type YES (capital letters) and press Enter to continue: ").strip()
                except EOFError:
                    ans = ""
                if ans != "YES":
                    print("Not confirmed. Nothing was traded.")
                    return 2
            open(confirmed, "w").write(iso())
        if os.path.exists(stopfile) and not args.once:
            print("A STOP request is still pending (state\\STOP). Start the desk with START.bat, which clears it.")
            return 0
        open(pidfile, "w").write(str(os.getpid()))
        return run(cfg, args, live, pf.client if live else None, flag, stopfile)
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass
        inst.release()


def run(cfg, args, live, client, flag, stopfile):
    global STOP
    data = Data(cfg)
    lg = Logger(cfg)
    risk = Risk(cfg, data, lg)
    if live:
        from desk.live import LiveBroker

        broker = LiveBroker(cfg, client, data, lg)
        try:
            for line in broker.reconcile(risk):
                print("[SYNC]", line)
                lg.log.info("reconcile: %s", line)
        except Exception as e:
            lg.log.error("reconcile failed: %s", redact(e))
            print("Could not sync with the Binance account:", redact(e))
            return 3
        if broker.foreign:
            lg.log.info("foreign holdings (ignored): %s", broker.foreign)
        cfg["start_balance"] = broker.s["start_equity"] or cfg["start_balance"]
    else:
        broker = PaperBroker(cfg)
    watcher, filt, analyst = Watcher(cfg, data, lg), Filter(cfg, data, lg), Analyst(cfg, lg)
    execu = Executor(cfg, data, broker, risk, lg)
    closefile = CTL.close_path(cfg)
    pausefile = CTL.pause_path(cfg)
    if os.path.exists(pausefile):
        print("NOTE: new trades are PAUSED (state\\PAUSE exists). Run RESUME.bat to allow new entries.")
        lg.log.info("started PAUSED (no new entries) - RESUME.bat lifts it")
    target = cfg["target_equity"]
    t0 = time.time()
    last = {"cycle": None}
    bids = {}
    state = "running"
    next_scan = 0
    errs = 0

    def write_status(extra=None):
        eq = broker.equity(bids)
        pos = []
        for s, p in broker.positions.items():
            b = bids.get(s, p["entry"])
            pos.append(
                {
                    **{k: p.get(k) for k in ("symbol", "qty", "entry", "stop", "initial_stop", "peak", "trailing", "score", "entry_ts")},
                    "mark_bid": b,
                    "unrealized_pnl": p["qty"] * b - (p["cost"] if live else p["qty"] * p["entry"] + p["entry_fee"]),
                    "unrealized_pct": (b / p["entry"] - 1) * 100,
                }
            )
        lg.status(
            {
                "ts": iso(),
                "mode": cfg["mode"],
                "state": state,
                "pid": os.getpid(),
                "uptime_sec": int(time.time() - t0),
                "start_balance": cfg["start_balance"],
                "equity": round(eq, 6),
                "cash": round(broker.cash, 6),
                "pnl": round(eq - cfg["start_balance"], 6),
                "pnl_pct": round((eq / cfg["start_balance"] - 1) * 100, 3),
                "realized_pnl": round(broker.s["realized_pnl"], 6),
                "fees_paid": round(broker.s["fees_paid"], 6),
                "target_equity": target,
                "target_progress_pct": round(eq / target * 100, 5),
                "open_positions": pos,
                "trades_closed": broker.s["closed"],
                "wins": broker.s["wins"],
                "last_cycle": last["cycle"],
                "data_health": filt.health,
                "paused": os.path.exists(pausefile),
                **(extra or {}),
            }
        )

    def log_events(evs=None):
        if live:
            for ev in broker.drain_events() if evs is None else evs:
                lg.trade(ev)
                if ev["event"] == "exit":
                    bids.pop(ev["symbol"], None)

    def handle_control():
        """Manual close request (CLOSE-TRADE.bat or the visualizer button): sell the open positions now. Always answers with state/close_result.json."""
        req, problem = CTL.read_request(closefile)
        if problem == "none":
            return
        try:
            if problem:
                results = [{"symbol": "-", "status": "skipped", "detail": problem}]
                lg.log.warning("close request ignored: %s", problem)
            else:
                lg.log.info(
                    "MANUAL CLOSE requested via %s (symbols=%s, pause_after=%s)",
                    req.get("source"),
                    req.get("symbols") or "all",
                    bool(req.get("pause_after")),
                )
                if live:
                    try:
                        broker.refresh()
                    except Exception as e:
                        lg.log.warning("account refresh failed: %s", redact(e))
                    results = broker.close_all(req.get("symbols"))
                    log_events()
                    if not results:
                        results = [{"symbol": "-", "status": "skipped", "detail": "no open position"}]
                else:
                    results = []
                    bt = data.book_tickers()
                    for s in list(broker.positions):
                        if req.get("symbols") and s not in req["symbols"]:
                            continue
                        try:
                            bid = float(bt[s]["bidPrice"]) if s in bt else bids.get(s, broker.positions[s]["entry"])
                            ev = execu.exit(s, bid, "manual", bids)
                            lg.trade(ev)
                            bids.pop(s, None)
                            results.append(
                                {
                                    "symbol": s,
                                    "status": "closed",
                                    "detail": "sold at the simulated bid; recorded with reason 'manual'",
                                    "pnl": ev["pnl"],
                                    "pnl_pct": ev["pnl_pct"],
                                    "price": bid,
                                    "qty": ev["qty"],
                                }
                            )
                        except Exception as e:
                            results.append({"symbol": s, "status": "failed", "detail": f"{e.__class__.__name__}: {e}"})
                    if not results:
                        results = [{"symbol": "-", "status": "skipped", "detail": "no open position"}]
                for r in results:
                    lg.log.info("manual close %s: %s - %s", r["symbol"], r["status"], r.get("detail"))
                if req.get("pause_after"):
                    CTL.set_pause(cfg, True)
                    lg.log.info("PAUSED after the manual close (no new entries until RESUME.bat)")
        finally:
            CTL.finish_request(
                cfg,
                req,
                results if "results" in locals() else [{"symbol": "-", "status": "failed", "detail": "internal error, see desk.log"}],
                paused=os.path.exists(pausefile),
            )
        write_status()

    lg.log.info("desk started mode=%s equity=%.4f", cfg["mode"], broker.equity({}))
    print(f"Desk is running ({cfg['mode'].upper()}). Equity {broker.equity({}):.4f}. Press Ctrl+C or run STOP.bat to stop.")
    while not STOP:
        if os.path.exists(stopfile) and not args.once:
            lg.log.info("STOP file found")
            break
        now = time.time()
        try:
            handle_control()
            # --- Risk: monitor open positions every tick
            if live and broker.positions:
                try:
                    broker.refresh()
                except Exception as e:
                    lg.log.warning("account refresh failed: %s", redact(e))
            if broker.positions:
                bt = data.book_tickers()
                for s in list(broker.positions):
                    if s in bt:
                        bids[s] = float(bt[s]["bidPrice"])
                if live:
                    log_events(broker.manage(bids, risk))  # exchange holds the stops; this tracks fills, trails and safety exits
                else:
                    for s in list(broker.positions):
                        hit = risk.monitor(broker.positions[s], bids.get(s, broker.positions[s]["entry"]))
                        if hit:
                            ev = execu.exit(s, hit[0], hit[1], bids)
                            lg.trade(ev)
                            bids.pop(s, None)
                        else:
                            broker.save()
            elif live:
                try:
                    broker.refresh()
                except Exception as e:
                    lg.log.warning("account refresh failed: %s", redact(e))
            eq = broker.equity(bids)
            if eq >= target:
                state = "target_reached"
                write_status()
                open(flag, "w").write(iso())
                lg.log.info("TARGET REACHED equity=%.2f", eq)
                print(f"TARGET REACHED: equity {eq:,.2f}. The desk has stopped placing trades.")
                break
            # --- Watcher -> Filter -> Analyst -> Executor
            if now >= next_scan:
                next_scan = now + cfg["loop"]["scan_interval_sec"]
                cyc = lg.counters["cycles"] + 1
                cands = watcher.scan()
                survivors = filt.run(cands, cyc)
                ranked = analyst.run(survivors, cyc)
                for c in cands:
                    if c.symbol in broker.positions:
                        bids[c.symbol] = c.bid
                if os.path.exists(pausefile):
                    entry = None
                    lg.skip(cyc, "executor", "PAUSED by you (PAUSE.bat): scanning continues but no new trade is opened")
                else:
                    entry = execu.enter(ranked, bids, cyc)
                if entry:
                    lg.trade(entry)
                    bids[entry["symbol"]] = entry["price"]
                if live:
                    log_events()
                summ = {
                    "cycle": cyc,
                    "candidates": len(cands),
                    "filter_survivors": len(survivors),
                    "analyst_passed": len(ranked),
                    "watched": [c.symbol for c in cands],
                    "killed_filter": len(cands) - len(survivors),
                    "ranked": [{"symbol": c.symbol, "score": c.score, **c.factors} for c in ranked],
                    "entry": entry["symbol"] if entry else None,
                    "equity": round(broker.equity(bids), 6),
                }
                lg.cycle(summ)
                last["cycle"] = {k: summ[k] for k in ("cycle", "candidates", "filter_survivors", "analyst_passed", "entry", "equity")}
                lg.log.info(
                    "cycle %d: %d cands -> %d survive filter -> %d pass score; entry=%s", cyc, len(cands), len(survivors), len(ranked), summ["entry"]
                )
                if args.once:
                    write_status()
                    break
            write_status()
            errs = 0
        except Exception:
            errs += 1
            lg.log.error("tick error:\n%s", redact(traceback.format_exc()))
            next_scan = min(next_scan, time.time() + 30)
            try:
                write_status({"last_error": iso()})
            except Exception:
                pass
            if errs >= 20:
                lg.log.error("20 errors in a row - exiting so the supervisor restarts with a fresh pre-flight")
                state = "error_exit"
                break
        for _ in range(cfg["loop"]["monitor_interval_sec"]):
            if STOP or os.path.exists(stopfile) or os.path.exists(closefile):
                break  # a close request is answered within a second
            time.sleep(1)
    state = state if state in ("target_reached", "error_exit") else "stopped"
    try:
        write_status()
    except Exception:
        pass
    lg.save_counters()
    lg.log.info("desk exiting: %s", state)
    if live and broker.positions:
        print("NOTE: open positions keep their stop-loss orders on Binance while the desk is off.")
    return 1 if state == "error_exit" else 0


if __name__ == "__main__":
    sys.exit(main())

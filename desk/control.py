"""Manual controls shared by the desk, control.py (CLOSE-TRADE.bat / PAUSE.bat / RESUME.bat) and the visualizer button.
Everything is plain files in the desk's state folder, so any of them can ask and the running desk answers:
  state/close_request.json   "sell the open position(s) now"            (written by CLOSE-TRADE.bat or the visualizer; the desk deletes it when done)
  state/close_result.json    what happened (written by the desk or by the direct close)
  state/PAUSE                exists = no NEW entries (open positions stay managed and keep their stop-loss)"""

import json
import os
import time

from .util import atomic_write_json, iso

REQUEST_MAX_AGE_SEC = 15 * 60  # an old request (desk was off) is ignored instead of selling a trade later by surprise


def _state(cfg):
    return os.path.join(cfg["_data"], "state")


def close_path(cfg):
    return os.path.join(_state(cfg), "close_request.json")


def result_path(cfg):
    return os.path.join(_state(cfg), "close_result.json")


def pause_path(cfg):
    return os.path.join(_state(cfg), "PAUSE")


def write_request(cfg, source="bat", symbols=None, pause_after=False):
    os.makedirs(_state(cfg), exist_ok=True)
    req = {
        "ts": iso(),
        "ts_epoch": time.time(),
        "source": source,
        "reason": "manual",
        "symbols": [s.upper() for s in (symbols or [])] or None,
        "pause_after": bool(pause_after),
    }
    atomic_write_json(close_path(cfg), req)
    return req


def read_request(path):
    """-> (request_dict, problem). problem is "none" when there is no request, "" when the request is good, else a plain-English reason to ignore it."""
    if not os.path.exists(path):
        return None, "none"
    try:
        with open(path, encoding="utf-8") as f:
            req = json.load(f)
        if not isinstance(req, dict):
            raise ValueError("not an object")
    except Exception:
        return {"source": "unreadable file", "symbols": None}, ""  # a broken file still means "the user asked to close": act on everything
    age = time.time() - float(req.get("ts_epoch") or os.path.getmtime(path))
    if age > REQUEST_MAX_AGE_SEC:
        return (
            req,
            f"the close request is {int(age // 60)} minutes old (the desk was probably off) and was ignored for safety; ask again if you still want to close",
        )
    syms = req.get("symbols")
    req["symbols"] = [str(s).upper() for s in syms] if isinstance(syms, list) and syms else None
    return req, ""


def finish_request(cfg, req, results, paused=False):
    """Write the answer FIRST, then remove the request (so nobody ever sees 'request gone, no answer')."""
    atomic_write_json(result_path(cfg), {"ts": iso(), "ts_epoch": time.time(), "request": req, "results": results, "paused": paused})
    p = close_path(cfg)
    for _ in range(5):
        try:
            os.remove(p)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.2)
    try:
        os.replace(p, p + ".done")
    except OSError:
        pass


def set_pause(cfg, on):
    p = pause_path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if on:
        with open(p, "w", encoding="utf-8") as f:
            f.write("paused " + iso() + "\n")
    else:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    return os.path.exists(p)

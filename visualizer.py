#!/usr/bin/env python3
"""Crypto Desk live visualizer: a tiny READ-ONLY local web server (Python standard library only).
   python visualizer.py                       -> http://127.0.0.1:8765  (this PC only)
   python visualizer.py --host 0.0.0.0        -> also reachable from a phone on the same Wi-Fi
   python visualizer.py --mode paper|live|auto, --port N, --only-if-new
   --open        open the screen as a standalone app window (Edge/Chrome "--app" mode: no address bar, no tabs),
                 falling back to the default browser. --tab opens a normal browser tab instead.
It only READS status.json, logs/*.jsonl, logs/desk.log and state/*. It never writes files, never talks to Binance, never sees your keys."""

import argparse
import calendar
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = "crypto-desk-visualizer"
HTML_PATH = os.path.join(ROOT, "visualizer.html")
ASSETS = os.path.join(ROOT, "assets")
APP_W, APP_H = 1400, 900
# URL path -> (file, content type). Fixed allow-list: nothing else on disk is ever served.
STATIC = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/icon-maskable-512.png": ("icon-maskable-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/favicon.ico": ("crypto-desk.ico", "image/x-icon"),
}
LOWER_BOUND = {"volume", "depth", "score"}  # killed because value < threshold
UPPER_BOUND = {"spread", "funding"}  # killed because |value| > threshold


# ----------------------------------------------------------------------------- tolerant file readers
def read_json(path):
    for _ in range(2):  # the desk replaces files atomically; one retry covers a rare race
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            time.sleep(0.05)
    return None


def tail_text(path, max_bytes):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial first line
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


_cache = {}


def read_jsonl(path, max_bytes=400_000):
    """Parsed tail of a .jsonl file; cached by (mtime, size) so polling every second is cheap. Bad lines are skipped."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    out = []
    for line in tail_text(path, max_bytes).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    _cache[path] = (key, out)
    return out


def read_csv(path, limit=60):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    out = []
    for r in rows[-limit:]:
        for k in ("qty", "price", "notional", "fee", "pnl", "pnl_pct", "score", "stop", "cash_after", "equity_after"):
            try:
                r[k] = float(r[k]) if r.get(k) not in (None, "") else None
            except ValueError:
                r[k] = None
        out.append(r)
    return out


def iso_to_epoch(s):
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+) (.*)$")


def parse_log(path, limit=60):
    out = []
    for line in tail_text(path, 60_000).splitlines():
        m = LOG_RE.match(line)
        if not m:
            continue
        try:
            t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))  # desk.log is written in this PC's local time
        except Exception:
            continue
        out.append({"t": t, "level": m.group(2), "msg": m.group(3)})
    return out[-limit:]


def load_config():
    try:
        import tomllib

        own = os.path.join(ROOT, "config.toml")
        with open(own if os.path.exists(own) else os.path.join(ROOT, "config.example.toml"), "rb") as f:
            c = tomllib.load(f)
    except Exception:
        c = {}
    g = lambda *k, d=None: _dig(c, k, d)
    return {
        "scan_interval_sec": g("loop", "scan_interval_sec", d=300),
        "monitor_interval_sec": g("loop", "monitor_interval_sec", d=30),
        "target_equity": g("target_equity", d=2000.0),
        "min_score": g("analyst", "min_score", d=0.6),
        "stop_loss_pct": g("risk", "stop_loss_pct", d=8.0),
        "trail_activate_pct": g("risk", "trail_activate_pct", d=10.0),
        "trail_pct": g("risk", "trail_pct", d=5.0),
        "allowed_symbols": [str(s).upper() for s in (g("watcher", "allowed_symbols", d=[]) or [])],
        "majors": [str(s).upper() for s in (g("watcher", "majors", d=[]) or [])],
        "weights": {
            "momentum": g("analyst", "w_momentum", d=0.4),
            "trend": g("analyst", "w_trend", d=0.35),
            "volume": g("analyst", "w_volume", d=0.25),
        },
    }


def _dig(c, keys, d):
    for k in keys:
        if not isinstance(c, dict) or k not in c:
            return d
        c = c[k]
    return c


# ----------------------------------------------------------------------------- live prices (public data only)
DATA_API = "https://data-api.binance.vision"  # public market data, no key, works from restricted regions
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
_tk = {"at": 0.0, "key": None, "data": None, "bad": set(), "err": None}
_tk_lock = threading.Lock()


def _http_json(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "CryptoDeskViz/1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ticker_symbols(cfg, cyc):
    """Which coins the tape shows: the allowlist if set, else the last scan's watched coins, else the majors."""
    syms = list(cfg.get("allowed_symbols") or [])
    if not syms and cyc:
        syms = [f["symbol"] for f in (cyc.get("flow") or [])]
    if not syms:
        syms = list(cfg.get("majors") or ["BTCUSDT", "ETHUSDT", "BNBUSDT"])
    out = []
    for s in syms:
        s = str(s).upper()
        if SYMBOL_RE.match(s) and s not in out:
            out.append(s)
    return out[:80]


def fetch_tickers(symbols, ttl=5.0, now=time.time, http=None):
    """24h ticker for the given symbols from the public data API. Cached for `ttl` seconds. Never raises: returns {ok, data|error}."""
    http = http or _http_json
    symbols = [s for s in dict.fromkeys(str(s).upper() for s in symbols) if SYMBOL_RE.match(s)][:80]
    if not symbols:
        return {"ok": False, "error": "no symbols"}
    with _tk_lock:
        key = ",".join(symbols)
        t = now()
        if _tk["key"] == key and _tk["data"] is not None and t - _tk["at"] < ttl:
            return {"ok": True, "data": _tk["data"], "cached": True}
        want = [s for s in symbols if s not in _tk["bad"]]

        def shape(rows):
            return [
                {"s": r["symbol"], "p": r["lastPrice"], "c": r["priceChangePercent"], "q": r.get("quoteVolume")}
                for r in rows
                if r.get("symbol") in set(want)
            ]

        try:
            rows = http(DATA_API + "/api/v3/ticker/24hr?symbols=" + urllib.parse.quote(json.dumps(want, separators=(",", ":"))))
            data = shape(rows)
        except urllib.error.HTTPError as e:
            if e.code != 400:
                _tk["err"] = f"HTTP {e.code}"
                return {"ok": False, "error": _tk["err"]}
            try:  # a symbol on the list is not (or no longer) listed: pull everything once and learn which
                rows = http(DATA_API + "/api/v3/ticker/24hr", timeout=12)
                have = {r["symbol"] for r in rows}
                _tk["bad"] |= {s for s in want if s not in have}
                want = [s for s in want if s in have]
                data = shape(rows)
            except Exception as e2:
                _tk["err"] = e2.__class__.__name__
                return {"ok": False, "error": _tk["err"]}
        except Exception as e:
            _tk["err"] = e.__class__.__name__
            return {"ok": False, "error": _tk["err"]}
        _tk.update(at=t, key=key, data=data, err=None)
        return {"ok": True, "data": data}


# ----------------------------------------------------------------------------- shaping
def fmt_reason(r):
    chk, v, th, det = r.get("check", "?"), r.get("value"), r.get("threshold"), r.get("detail") or ""
    try:
        if chk in LOWER_BOUND:
            return f"{chk} {v:g} < {th:g}"
        if chk in UPPER_BOUND:
            return f"{chk} {abs(v):g} > {th:g}"
    except Exception:
        pass
    return f"{chk}: {det[:90]}" if det else str(chk)


def parse_factors(detail):
    out = {}
    for m in re.finditer(r"(\w+)=(-?[\d.]+)", detail or ""):
        try:
            out[m.group(1)] = float(m.group(2))
        except ValueError:
            pass
    return out


_samples = {}  # in-memory equity samples (status.json only holds "now"), per data dir


def equity_series(key, cycles, trades, status):
    pts = {}
    for c in cycles:
        t = iso_to_epoch(c.get("ts", ""))
        if t and c.get("equity") is not None:
            pts[t] = c["equity"]
    for tr in trades:
        t = iso_to_epoch(tr.get("ts", "")) if isinstance(tr.get("ts"), str) else None
        if t and tr.get("equity_after") not in (None, ""):
            pts[t] = tr["equity_after"]
    if status and status.get("equity") is not None:
        t = iso_to_epoch(status.get("ts", ""))
        if t:
            s = _samples.setdefault(key, [])
            if not s or s[-1][0] != t:
                s.append((t, status["equity"]))
                del s[:-3000]
    for t, e in _samples.get(key, []):
        pts[t] = e
    return sorted(pts.items())[-600:]


def build_state(mode, data_dir, default_mode):
    now = time.time()
    cfg = load_config()
    status = read_json(os.path.join(data_dir, "status.json"))
    logs = os.path.join(data_dir, "logs")
    cycles = read_jsonl(os.path.join(logs, "cycles.jsonl"), 600_000)
    kills = read_jsonl(os.path.join(logs, "kills.jsonl"), 400_000)
    trades = read_jsonl(os.path.join(logs, "trades.jsonl"), 200_000) or read_csv(os.path.join(logs, "trades.csv"))
    logl = parse_log(os.path.join(logs, "desk.log"))
    sup = [l for l in tail_text(os.path.join(logs, "supervisor.log"), 4000).splitlines() if l.strip()][-3:]

    st_t = iso_to_epoch(status.get("ts", "")) if status else None
    age = (now - st_t) if st_t else None
    stale_after = max(180, 6 * cfg["monitor_interval_sec"])
    if not status:
        desk_state = "no_data"
    elif status.get("state") in ("target_reached", "stopped", "error_exit"):
        desk_state = status["state"]
    elif age is not None and age > stale_after:
        desk_state = "offline"
    else:
        desk_state = "running"

    last = cycles[-1] if cycles else None
    last_n = last.get("cycle") if last else (max((k.get("cycle", 0) for k in kills), default=None) if kills else None)
    cyc = None
    if last_n is not None:
        ck = [k for k in kills if k.get("cycle") == last_n]
        by_sym = {}
        for k in ck:
            by_sym.setdefault(k["symbol"], k)
        ranked = (last or {}).get("ranked", [])
        ranked_syms = [r["symbol"] for r in ranked]
        watched = (last or {}).get("watched") or list(by_sym) + ranked_syms
        entry = (last or {}).get("entry")
        flow = []
        miss = max(
            0, ((last or {}).get("candidates", len(watched)) - (last or {}).get("filter_survivors", 0)) - sum(1 for k in ck if k["stage"] == "filter")
        )
        for s in watched:
            k = by_sym.get(s)
            if s == entry:
                flow.append({"symbol": s, "fate": "entered", "score": next((r["score"] for r in ranked if r["symbol"] == s), None)})
            elif s in ranked_syms:
                flow.append({"symbol": s, "fate": "passed", "score": next((r["score"] for r in ranked if r["symbol"] == s), None)})
            elif k and k["stage"] == "filter":
                rs = k.get("reasons", [])
                flow.append({"symbol": s, "fate": "filter_kill", "checks": [r["check"] for r in rs], "why": [fmt_reason(r) for r in rs]})
            elif k:
                flow.append({"symbol": s, "fate": "analyst_kill", "score": k.get("score"), "why": [fmt_reason(r) for r in k.get("reasons", [])]})
            else:
                if miss > 0:
                    miss -= 1
                    flow.append({"symbol": s, "fate": "filter_kill", "checks": ["filter"], "why": ["filter"]})
                else:
                    flow.append({"symbol": s, "fate": "analyst_kill", "score": None, "why": ["score"]})
        scores = []
        for k in ck:
            if k["stage"] == "analyst":
                scores.append(
                    {
                        "symbol": k["symbol"],
                        "score": k.get("score"),
                        "factors": parse_factors((k.get("reasons") or [{}])[0].get("detail")),
                        "passed": False,
                    }
                )
        for r in ranked:
            scores.append(
                {
                    "symbol": r["symbol"],
                    "score": r.get("score"),
                    "passed": True,
                    "factors": {x: r[x] for x in ("momentum", "trend", "volume", "r6h_pct", "r24h_pct", "rsi14", "vol_ratio_10d") if x in r},
                }
            )
        scores.sort(key=lambda x: -(x["score"] or 0))
        fcount = {}
        for k in ck:
            if k["stage"] == "filter":
                for r in k.get("reasons", []):
                    fcount[r["check"]] = fcount.get(r["check"], 0) + 1
        cyc = {
            "n": last_n,
            "t": iso_to_epoch(last["ts"]) if last else None,
            "candidates": (last or {}).get("candidates"),
            "filter_survivors": (last or {}).get("filter_survivors"),
            "analyst_passed": (last or {}).get("analyst_passed"),
            "entry": entry,
            "flow": flow,
            "scores": scores,
            "filter_kill_counts": fcount,
            "equity": (last or {}).get("equity"),
        }

    events = []

    def ev(t, kind, text, sym=None, tone=None):
        if t is None:
            return
        events.append(
            {"id": hashlib.md5(f"{kind}|{t}|{text}".encode()).hexdigest()[:12], "t": t, "kind": kind, "text": text, "sym": sym, "tone": tone}
        )

    for c in cycles[-25:]:
        ev(
            iso_to_epoch(c.get("ts", "")),
            "cycle",
            f"Cycle {c.get('cycle')}: {c.get('candidates')} watched -> {c.get('filter_survivors')} passed filter -> {c.get('analyst_passed')} passed analyst"
            + (f" -> BOUGHT {c['entry']}" if c.get("entry") else " -> no entry"),
            tone="info",
        )
    for k in kills[-140:]:
        why = "; ".join(fmt_reason(r) for r in k.get("reasons", []))
        ev(iso_to_epoch(k.get("ts", "")), "kill", f"{k.get('symbol')} killed at {k.get('stage')}: {why}", k.get("symbol"), "kill")
    for tr in trades[-40:]:
        t = iso_to_epoch(tr["ts"]) if isinstance(tr.get("ts"), str) else None
        e = tr.get("event")
        if e == "entry":
            ev(
                t,
                "trade",
                f"BUY {tr.get('symbol')} qty {tr.get('qty')} @ {tr.get('price')} | stop {tr.get('stop')} | score {tr.get('score')}",
                tr.get("symbol"),
                "buy",
            )
        elif e == "exit":
            ev(
                t,
                "trade",
                f"SELL {tr.get('symbol')} @ {tr.get('price')} | {tr.get('reason')} | P&L {tr.get('pnl') if tr.get('pnl') is not None else '?'}",
                tr.get("symbol"),
                "win" if (tr.get("pnl") or 0) > 0 else "loss",
            )
        elif e == "stop_move":
            ev(t, "trade", f"TRAIL {tr.get('symbol')}: {tr.get('reason')}", tr.get("symbol"), "trail")
    for l in logl:
        if re.match(r"cycle \d+:", l["msg"]) or l["msg"].startswith("TRADE "):
            continue
        ev(
            l["t"],
            "system",
            f"[{l['level']}] {l['msg'][:220]}",
            tone="bad" if l["level"] in ("ERROR", "CRITICAL") else ("warn" if l["level"] == "WARNING" else "sys"),
        )
    events.sort(key=lambda e: e["t"], reverse=True)
    events = events[:220]

    counters = (status or {}).get("counters") or {}
    if not counters.get("kills_by_check") and kills:
        kc = {}
        for k in kills:
            for r in k.get("reasons", []):
                kc[r["check"]] = kc.get(r["check"], 0) + 1
        counters = {"kills_by_check": kc, "kills_total": len(kills), "kills_by_stage": {}}

    trades_view = []
    for tr in trades[-30:]:
        trades_view.append({k: tr.get(k) for k in ("ts", "event", "symbol", "side", "qty", "price", "pnl", "pnl_pct", "reason", "stop")})
    sdir = os.path.join(data_dir, "state")
    crq = read_json(os.path.join(sdir, "close_request.json"))
    crs = read_json(os.path.join(sdir, "close_result.json"))
    close_pending = (
        {"ts": crq.get("ts"), "age_sec": max(0, now - float(crq.get("ts_epoch") or now)), "pause_after": bool(crq.get("pause_after"))}
        if crq
        else ({"ts": None, "age_sec": 0, "pause_after": False} if os.path.exists(os.path.join(sdir, "close_request.json")) else None)
    )
    paused = bool((status or {}).get("paused")) or os.path.exists(os.path.join(sdir, "PAUSE"))
    return {
        "app": APP,
        "paused": paused,
        "close_pending": close_pending,
        "close_result": ({k: crs.get(k) for k in ("ts", "ts_epoch", "results", "paused")} if crs else None),
        "now": now,
        "mode": mode,
        "default_mode": default_mode,
        "data_dir": data_dir,
        "has_status": bool(status),
        "status": status,
        "status_age_sec": age,
        "desk_state": desk_state,
        "stale_after": stale_after,
        "config": cfg,
        "ticker_symbols": ticker_symbols(cfg, cyc),
        "cycle": cyc,
        "cycles_recent": [
            {k: c.get(k) for k in ("cycle", "ts", "candidates", "filter_survivors", "analyst_passed", "entry", "equity")} for c in cycles[-80:]
        ],
        "next_scan_at": ((cyc["t"] + cfg["scan_interval_sec"]) if cyc and cyc.get("t") else None),
        "equity_series": equity_series((mode, data_dir), cycles, trades, status),
        "trades": trades_view,
        "events": events,
        "counters": counters,
        "supervisor": sup,
        "files": {n: os.path.exists(os.path.join(logs, n)) for n in ("cycles.jsonl", "kills.jsonl", "trades.jsonl", "desk.log")},
    }


# ----------------------------------------------------------------------------- the ONE write: "close trade now" (asks the desk through a control file)
LOOPBACK_IPS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _is_local(client_ip, host_hdr):
    h = (host_hdr or "").strip().lower()
    name = h[: h.index("]") + 1] if h.startswith("[") else h.split(":")[0]
    return client_ip in LOOPBACK_IPS and name in LOOPBACK_HOSTS


def control_info(S, client_ip, host_hdr):
    """What the page may show for the close button: {enabled, token_required}. Local requests need nothing; the LAN needs --allow-control + the console token."""
    if getattr(S, "control_off", False):
        return {"enabled": False, "token_required": False}
    if _is_local(client_ip, host_hdr):
        return {"enabled": True, "token_required": False}
    if getattr(S, "control_token", None):
        return {"enabled": True, "token_required": True}
    return {"enabled": False, "token_required": False}


def control_auth(S, client_ip, host_hdr, origin, ctl_hdr, token_hdr):
    """-> (ok, reason). Local = the connection comes from this PC AND the Host header is loopback (blocks DNS rebinding). The browser must send the custom
    X-Desk-Control header (a web page on another site cannot do that without a CORS preflight, which this server never grants) and a same-origin Origin."""
    if getattr(S, "control_off", False):
        return False, "closing from the visualizer is switched off (--no-control)"
    if ctl_hdr != "1":
        return False, "missing X-Desk-Control header"
    if origin and urlparse(origin).netloc.lower() != (host_hdr or "").lower():
        return False, "cross-site request refused"
    if _is_local(client_ip, host_hdr):
        return True, ""
    tok = getattr(S, "control_token", None)
    if tok and token_hdr and hmac.compare_digest(str(token_hdr), tok):
        return True, ""
    return False, "not allowed from the network: use the PC itself, or start the visualizer with --allow-control and type the token it prints"


def request_close(state_dir, status, body):
    """Write state/close_request.json for the desk to pick up. -> (http_code, payload). Refuses when nothing is open or a request is already waiting."""
    held = [p.get("symbol") for p in ((status or {}).get("open_positions") or []) if p.get("symbol")]
    if not held:
        return 409, {"ok": False, "error": "there is no open position to close"}
    path = os.path.join(state_dir, "close_request.json")
    if os.path.exists(path):
        return 409, {"ok": False, "error": "a close request is already waiting for the desk"}
    syms = [s for s in (body.get("symbols") or []) if isinstance(s, str) and s.upper() in held] or None
    req = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts_epoch": time.time(),
        "source": "visualizer",
        "reason": "manual",
        "symbols": [s.upper() for s in syms] if syms else None,
        "pause_after": bool(body.get("pause_after")),
    }
    os.makedirs(state_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(req, f)
    os.replace(tmp, path)
    return 200, {"ok": True, "request": req}


# ----------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "CryptoDeskViz"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        S = self.server
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if S.loopback_only and host not in ("127.0.0.1", "localhost", "[::1]"):  # blocks DNS-rebinding tricks in local-only mode
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                with open(HTML_PATH, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path in STATIC:
                fn, ctype = STATIC[u.path]
                try:
                    with open(os.path.join(ASSETS, fn), "rb") as f:
                        return self._send(200, f.read(), ctype)
                except OSError:
                    return self._send(404, "missing asset", "text/plain")
            if u.path == "/api/ping":
                return self._send(200, json.dumps({"app": APP}), "application/json")
            if u.path == "/api/ticker":  # public prices only (no keys, no desk data); read-only GET
                q = (parse_qs(u.query).get("symbols") or [""])[0]
                syms = [s for s in q.upper().split(",") if SYMBOL_RE.match(s)] or ticker_symbols(load_config(), None)
                return self._send(200, json.dumps(fetch_tickers(syms)), "application/json")
            if u.path == "/api/state":
                mode = (parse_qs(u.query).get("mode") or [S.default_mode])[0]
                if mode not in ("live", "paper"):
                    mode = S.default_mode
                st = build_state(mode, S.dirs[mode], S.default_mode)
                st["modes"] = {m: {"exists": os.path.exists(os.path.join(d, "status.json"))} for m, d in S.dirs.items()}
                st["control"] = control_info(S, self.client_address[0], self.headers.get("Host"))
                return self._send(200, json.dumps(st, default=str), "application/json")
        except Exception as e:  # never crash the page: report and carry on
            return self._send(500, json.dumps({"error": f"{e.__class__.__name__}: {e}"}), "application/json")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        S = self.server
        host = self.headers.get("Host") or ""
        if S.loopback_only and host.split(":")[0].lower() not in ("127.0.0.1", "localhost", "[::1]"):
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        if u.path != "/api/close":
            return self._send(405, "read-only", "text/plain")  # the close button is the ONLY thing this server can do besides reading
        try:
            ok, why = control_auth(
                S, self.client_address[0], host, self.headers.get("Origin"), self.headers.get("X-Desk-Control"), self.headers.get("X-Desk-Token")
            )
            if not ok:
                return self._send(403, json.dumps({"ok": False, "error": why}), "application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                return self._send(413, json.dumps({"ok": False, "error": "too large"}), "application/json")
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            mode = body.get("mode") if body.get("mode") in ("live", "paper") else S.default_mode
            data_dir = S.dirs[mode]
            code, payload = request_close(os.path.join(data_dir, "state"), read_json(os.path.join(data_dir, "status.json")), body)
            return self._send(code, json.dumps(payload, default=str), "application/json")
        except Exception as e:
            return self._send(500, json.dumps({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), "application/json")

    do_PUT = do_DELETE = do_PATCH = lambda self: self._send(405, "read-only", "text/plain")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "YOUR-PC-IP"


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.load(r).get("app") == APP
    except Exception:
        return False


# ----------------------------------------------------------------------------- standalone app window
def browser_candidates(env=None, platform=None, which=shutil.which):
    """Ordered list of (name, path) to try: Microsoft Edge first, then Chrome. Paths are not checked for existence here."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    out = []
    if platform.startswith("win"):
        pf, pf86, la = (
            env.get("ProgramFiles", r"C:\Program Files"),
            env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            env.get("LocalAppData", ""),
        )
        out += [("edge", os.path.join(b, "Microsoft", "Edge", "Application", "msedge.exe")) for b in (pf86, pf)]
        out += [("chrome", os.path.join(b, "Google", "Chrome", "Application", "chrome.exe")) for b in (pf, pf86, la) if b]
        try:  # registry "App Paths" also knows where they live
            import winreg

            for name, exe in (("edge", "msedge.exe"), ("chrome", "chrome.exe")):
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        with winreg.OpenKey(hive, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + exe) as k:
                            out.append((name, winreg.QueryValue(k, None)))
                    except OSError:
                        pass
        except ImportError:
            pass
    elif platform == "darwin":
        out += [
            ("edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    for name, exe in (
        ("edge", "microsoft-edge"),
        ("edge", "msedge"),
        ("chrome", "google-chrome"),
        ("chrome", "chromium"),
        ("chrome", "chromium-browser"),
        ("chrome", "chrome"),
    ):
        p = which(exe)
        if p:
            out.append((name, p))
    return sorted(out, key=lambda x: 0 if x[0] == "edge" else 1)  # stable: Edge before Chrome, original order kept inside each


def app_command(exe, url):
    """The same as:  msedge --app=URL --window-size=1400,900"""
    return [exe, f"--app={url}", f"--window-size={APP_W},{APP_H}"]


def open_screen(url, as_app=True, exists=os.path.exists, spawn=None, fallback=webbrowser.open, **kw):
    """Open the visualizer: standalone app window (Edge, then Chrome), else the default browser. Returns how it was opened."""
    if as_app:
        spawn = spawn or _spawn
        for name, exe in browser_candidates(**kw):
            if exe and exists(exe):
                try:
                    spawn(app_command(exe, url))
                    return f"app:{name}"
                except OSError:
                    continue
    fallback(url)
    return "browser"


def _spawn(cmd):
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
    subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags
    ) if os.name == "nt" else subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True
    )


def pick_mode(mode, dirs):
    if mode != "auto":
        return mode
    best, bt = "live", -1
    for m, d in dirs.items():
        try:
            t = os.path.getmtime(os.path.join(d, "status.json"))
        except OSError:
            continue
        if t > bt:
            best, bt = m, t
    return best


def main(argv=None):
    ap = argparse.ArgumentParser(description="Crypto Desk live visualizer (read-only, plus the CLOSE TRADE NOW button)")
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = this PC only (default); 0.0.0.0 = whole Wi-Fi (phone)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mode", choices=["live", "paper", "auto"], default="auto")
    ap.add_argument("--live-dir", default=ROOT)
    ap.add_argument("--paper-dir", default=os.path.join(ROOT, "paper"))
    ap.add_argument("--open", action="store_true", help="open the screen as a standalone app window")
    ap.add_argument("--tab", action="store_true", help="with --open: use a normal browser tab instead of an app window")
    ap.add_argument("--only-if-new", action="store_true", help="if a visualizer is already running, do nothing")
    ap.add_argument(
        "--allow-control", action="store_true", help="with --host 0.0.0.0: let the phone press CLOSE TRADE NOW too (needs the token printed here)"
    )
    ap.add_argument("--no-control", action="store_true", help="remove the CLOSE TRADE NOW button entirely (fully read-only)")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    if already_running(a.port):
        print(f"The visualizer is already running: http://127.0.0.1:{a.port}")
        if a.open and not a.only_if_new:
            open_screen(f"http://127.0.0.1:{a.port}/?mode={pick_mode(a.mode, {'live': a.live_dir, 'paper': a.paper_dir})}", not a.tab)
        return 0
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        print(f"Cannot start the visualizer on port {a.port}: {e}. Another program may be using it; try --port 8766.")
        return 1
    srv.dirs = {"live": a.live_dir, "paper": a.paper_dir}
    srv.default_mode = pick_mode(a.mode, srv.dirs)
    srv.loopback_only = a.host in ("127.0.0.1", "localhost", "::1")
    srv.daemon_threads = True
    srv.control_off = a.no_control
    srv.control_token = secrets.token_urlsafe(9) if (a.allow_control and not srv.loopback_only and not a.no_control) else None
    url = f"http://127.0.0.1:{a.port}/?mode={srv.default_mode}"
    print("=" * 62)
    print("  CRYPTO DESK VISUALIZER  (read-only; the only action is the CLOSE TRADE NOW button)")
    print("=" * 62)
    print(f"  On this PC:   {url}")
    if not srv.loopback_only:
        print(f"  On your phone (same Wi-Fi):   http://{lan_ip()}:{a.port}/?mode={srv.default_mode}")
        print("  WARNING: anyone on this Wi-Fi network can see your balance and trades. They cannot change anything.")
    if a.no_control:
        print("  The CLOSE TRADE NOW button is switched off (--no-control).")
    elif srv.control_token:
        print(f"  CLOSE TRADE NOW is enabled for your phone. Token (type it on the phone when asked): {srv.control_token}")
    elif not srv.loopback_only:
        print("  The CLOSE TRADE NOW button works on this PC only (phone view is read-only). Start with --allow-control to enable it on the phone.")
    print("  Window title: Crypto Desk  (installable as an app: see docs/dashboard.md)")
    print("  Leave this window open. Close it (or press Ctrl+C) to stop the visualizer.")
    if a.open:
        threading.Timer(0.8, lambda: print("  Opened as:", open_screen(url, not a.tab))).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

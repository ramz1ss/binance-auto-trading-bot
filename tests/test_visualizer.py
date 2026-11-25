"""Visualizer: JSON shaping from real-format files, missing/corrupt files, read-only HTTP server, local-only host check."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import visualizer as V


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(text)


NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fixture(d, running=True):
    w(
        f"{d}/status.json",
        json.dumps(
            {
                "ts": NOW,
                "mode": "paper",
                "state": "running" if running else "stopped",
                "uptime_sec": 100,
                "start_balance": 7.0,
                "equity": 7.5,
                "cash": 1.0,
                "pnl": 0.5,
                "pnl_pct": 7.1,
                "realized_pnl": 0,
                "fees_paid": 0,
                "target_equity": 2000,
                "target_progress_pct": 0.01,
                "open_positions": [],
                "trades_closed": 0,
                "wins": 0,
                "data_health": {"funding": {"okx": "ok"}, "news": {"https://decrypt.co/feed": "error: x"}, "events": "3 in calendar"},
                "counters": {"kills_by_check": {"volume": 2, "score": 1}, "kills_total": 3, "kills_by_stage": {"filter": 2, "analyst": 1}},
            }
        ),
    )
    w(
        f"{d}/logs/cycles.jsonl",
        json.dumps(
            {
                "ts": NOW,
                "cycle": 5,
                "candidates": 4,
                "filter_survivors": 2,
                "analyst_passed": 1,
                "watched": ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"],
                "killed_filter": 2,
                "ranked": [{"symbol": "CCCUSDT", "score": 0.7, "momentum": 0.8, "trend": 0.7, "volume": 0.5}],
                "entry": None,
                "equity": 7.5,
            }
        )
        + "\n{broken\n",
    )
    w(
        f"{d}/logs/kills.jsonl",
        "\n".join(
            json.dumps(x)
            for x in [
                {
                    "ts": NOW,
                    "cycle": 5,
                    "stage": "filter",
                    "symbol": "AAAUSDT",
                    "score": None,
                    "reasons": [{"check": "volume", "value": 0.5, "threshold": 1.0, "detail": "x"}],
                },
                {
                    "ts": NOW,
                    "cycle": 5,
                    "stage": "filter",
                    "symbol": "BBBUSDT",
                    "score": None,
                    "reasons": [{"check": "news", "value": 1, "threshold": 0, "detail": "<script>alert(1)</script> hack"}],
                },
                {
                    "ts": NOW,
                    "cycle": 5,
                    "stage": "analyst",
                    "symbol": "DDDUSDT",
                    "score": 0.3,
                    "reasons": [{"check": "score", "value": 0.3, "threshold": 0.6, "detail": "momentum=0.3, trend=0.2, volume=0.4"}],
                },
            ]
        )
        + "\n",
    )
    w(f"{d}/logs/desk.log", "2026-11-20 13:08:59,906 INFO cycle 5: x\n2026-11-20 13:09:00,000 WARNING something odd\ngarbage line\n")


class T(unittest.TestCase):
    def test_state_from_files(self):
        d = tempfile.mkdtemp()
        fixture(d)
        s = V.build_state("paper", d, "paper")
        self.assertEqual(s["desk_state"], "running")
        self.assertEqual(s["cycle"]["n"], 5)
        fates = {f["symbol"]: f["fate"] for f in s["cycle"]["flow"]}
        self.assertEqual(fates, {"AAAUSDT": "filter_kill", "BBBUSDT": "filter_kill", "CCCUSDT": "passed", "DDDUSDT": "analyst_kill"})
        self.assertEqual([x["symbol"] for x in s["cycle"]["scores"]], ["CCCUSDT", "DDDUSDT"])
        self.assertEqual(s["cycle"]["scores"][1]["factors"]["momentum"], 0.3)
        kinds = {e["kind"] for e in s["events"]}
        self.assertTrue({"kill", "cycle", "system"} <= kinds)
        self.assertTrue(any("volume 0.5 < 1" in e["text"] for e in s["events"]))

    def test_missing_and_empty(self):
        s = V.build_state("live", tempfile.mkdtemp(), "live")
        self.assertEqual(s["desk_state"], "no_data")
        self.assertIsNone(s["cycle"])
        self.assertEqual(s["events"], [])
        d = tempfile.mkdtemp()
        w(f"{d}/status.json", "{not json")
        self.assertEqual(V.build_state("live", d, "live")["desk_state"], "no_data")
        d = tempfile.mkdtemp()
        w(f"{d}/logs/kills.jsonl", "")
        w(f"{d}/logs/cycles.jsonl", "")
        self.assertIsNone(V.build_state("live", d, "live")["cycle"])

    def test_stale_and_stopped(self):
        d = tempfile.mkdtemp()
        fixture(d)
        st = json.load(open(f"{d}/status.json"))
        st["ts"] = "2020-01-01T00:00:00Z"
        w(f"{d}/status.json", json.dumps(st))
        self.assertEqual(V.build_state("paper", d, "paper")["desk_state"], "offline")
        d = tempfile.mkdtemp()
        fixture(d, running=False)
        self.assertEqual(V.build_state("paper", d, "paper")["desk_state"], "stopped")

    def test_trades_csv_fallback(self):
        d = tempfile.mkdtemp()
        fixture(d)
        w(
            f"{d}/logs/trades.csv",
            "ts,event,symbol,side,qty,price,notional,fee,pnl,pnl_pct,reason,score,stop,cash_after,equity_after\n"
            + NOW
            + ",entry,SOLUSDT,BUY,0.04,150,6,0.006,,,x,0.9,138,1,7\n",
        )
        s = V.build_state("paper", d, "paper")
        self.assertTrue(any(e["kind"] == "trade" for e in s["events"]))

    def test_http_readonly_and_localonly(self):
        d = tempfile.mkdtemp()
        fixture(d)
        srv = V.ThreadingHTTPServer(("127.0.0.1", 0), V.Handler)
        srv.dirs = {"live": d, "paper": d}
        srv.default_mode = "paper"
        srv.loopback_only = True
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        try:
            self.assertIn("<title>Binance Auto Trading Bot (Crypto Desk)</title>", urllib.request.urlopen(base + "/").read().decode())
            js = json.load(urllib.request.urlopen(base + "/api/state?mode=paper"))
            self.assertEqual(js["cycle"]["n"], 5)
            self.assertEqual(json.load(urllib.request.urlopen(base + "/api/ping"))["app"], V.APP)
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(urllib.request.Request(base + "/api/state", data=b"x", method="POST"))
            self.assertEqual(c.exception.code, 405)
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(base + "/../config.toml")
            self.assertIn(c.exception.code, (404,))
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(urllib.request.Request(base + "/api/state", headers={"Host": "evil.example"}))
            self.assertEqual(c.exception.code, 403)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_app_window_launch_order_and_fallback(self):
        calls = []
        opened = []
        env = {"ProgramFiles": r"C:\PF", "ProgramFiles(x86)": r"C:\PF86", "LocalAppData": r"C:\LA"}
        nowhich = lambda x: None
        # Edge present -> Edge, with the exact flags
        edge = os.path.join(r"C:\PF86", "Microsoft", "Edge", "Application", "msedge.exe")
        chrome = os.path.join(r"C:\PF", "Google", "Chrome", "Application", "chrome.exe")
        how = V.open_screen(
            "http://127.0.0.1:8765/?mode=live",
            True,
            exists=lambda p: p in (edge, chrome),
            spawn=calls.append,
            fallback=opened.append,
            env=env,
            platform="win32",
            which=nowhich,
        )
        self.assertEqual(how, "app:edge")
        self.assertEqual(calls[-1], [edge, "--app=http://127.0.0.1:8765/?mode=live", "--window-size=1400,900"])
        self.assertEqual(opened, [])
        # no Edge -> Chrome app mode
        how = V.open_screen(
            "http://x/", True, exists=lambda p: p == chrome, spawn=calls.append, fallback=opened.append, env=env, platform="win32", which=nowhich
        )
        self.assertEqual(how, "app:chrome")
        self.assertEqual(calls[-1][0], chrome)
        # neither -> default browser
        how = V.open_screen(
            "http://x/", True, exists=lambda p: False, spawn=calls.append, fallback=opened.append, env=env, platform="win32", which=nowhich
        )
        self.assertEqual((how, opened), ("browser", ["http://x/"]))

        # spawn failure on Edge -> falls through to Chrome
        def flaky(cmd):
            if "msedge" in cmd[0]:
                raise OSError("boom")
            calls.append(cmd)

        self.assertEqual(
            V.open_screen(
                "http://x/", True, exists=lambda p: p in (edge, chrome), spawn=flaky, fallback=opened.append, env=env, platform="win32", which=nowhich
            ),
            "app:chrome",
        )
        # --tab -> plain browser
        n = len(calls)
        V.open_screen("http://y/", False, exists=lambda p: True, spawn=calls.append, fallback=opened.append)
        self.assertEqual((len(calls), opened[-1]), (n, "http://y/"))

    def test_pwa_assets_served(self):
        d = tempfile.mkdtemp()
        fixture(d)
        srv = V.ThreadingHTTPServer(("127.0.0.1", 0), V.Handler)
        srv.dirs = {"live": d, "paper": d}
        srv.default_mode = "paper"
        srv.loopback_only = True
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            r = urllib.request.urlopen(base + "/manifest.webmanifest")
            m = json.load(r)
            self.assertIn("manifest+json", r.headers["Content-Type"])
            self.assertEqual((m["name"], m["display"], m["start_url"]), ("Crypto Desk", "standalone", "/"))
            sizes = {i["sizes"] for i in m["icons"]}
            self.assertTrue({"192x192", "512x512"} <= sizes)
            for i in m["icons"]:
                b = urllib.request.urlopen(base + i["src"]).read()
                self.assertEqual(b[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(urllib.request.urlopen(base + "/favicon.ico").read()[:4], b"\x00\x00\x01\x00")  # real .ico
            self.assertIn("fetch", urllib.request.urlopen(base + "/sw.js").read().decode())
            html = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("<title>Binance Auto Trading Bot (Crypto Desk)</title>", html)
            self.assertIn('rel="manifest"', html)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_shortcut_script_and_bats_reference_existing_files(self):
        root = os.path.dirname(V.__file__)
        win = os.path.join(root, "windows")
        ps = open(os.path.join(win, "create_shortcut.ps1"), encoding="utf-8").read()
        self.assertIn("WScript.Shell", ps)
        self.assertIn("crypto-desk.ico", ps)
        self.assertIn("VISUALIZER.bat", ps)
        for f in ("assets/crypto-desk.ico", "windows/VISUALIZER.bat", "windows/CREATE-SHORTCUT.bat", "visualizer.html"):
            self.assertTrue(os.path.exists(os.path.join(root, f)), f)
        start = open(os.path.join(win, "START.bat"), encoding="utf-8").read()
        self.assertIn("VISUALIZER.bat", start)

    def test_ticker_symbols_prefers_allowlist_then_scan_then_majors(self):
        self.assertEqual(
            V.ticker_symbols({"allowed_symbols": ["btcusdt", "SOLUSDT", "BTCUSDT", "bad symbol!"], "majors": ["ETHUSDT"]}, None),
            ["BTCUSDT", "SOLUSDT"],
        )
        self.assertEqual(V.ticker_symbols({"allowed_symbols": [], "majors": ["ETHUSDT"]}, {"flow": [{"symbol": "XRPUSDT"}]}), ["XRPUSDT"])
        self.assertEqual(V.ticker_symbols({"allowed_symbols": [], "majors": ["ETHUSDT"]}, None), ["ETHUSDT"])
        self.assertIn("ticker_symbols", V.build_state("paper", tempfile.mkdtemp(), "paper"))

    def test_fetch_tickers_shapes_caches_and_never_raises(self):
        V._tk.update(at=0.0, key=None, data=None, bad=set(), err=None)
        calls = []

        def ok(url, timeout=6):
            calls.append(url)
            return [{"symbol": "BTCUSDT", "lastPrice": "100.0", "priceChangePercent": "1.5", "quoteVolume": "9"}]

        r = V.fetch_tickers(["BTCUSDT"], http=ok)
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"][0], {"s": "BTCUSDT", "p": "100.0", "c": "1.5", "q": "9"})
        self.assertTrue(calls[0].startswith(V.DATA_API + "/api/v3/ticker/24hr?symbols="))
        r2 = V.fetch_tickers(["BTCUSDT"], http=ok)
        self.assertTrue(r2.get("cached"))
        self.assertEqual(len(calls), 1)  # 5 s cache
        # a delisted symbol on the list: the API answers 400 -> everything is fetched once and the bad symbol is remembered
        V._tk.update(at=0.0, key=None, data=None, bad=set())
        seq = []

        def flaky(url, timeout=6):
            seq.append(url)
            if "symbols=" in url:
                raise urllib.error.HTTPError(url, 400, "Invalid symbol", {}, None)
            return [
                {"symbol": "BTCUSDT", "lastPrice": "1", "priceChangePercent": "0"},
                {"symbol": "ETHUSDT", "lastPrice": "2", "priceChangePercent": "0"},
            ]

        r = V.fetch_tickers(["BTCUSDT", "GONEUSDT"], http=flaky)
        self.assertTrue(r["ok"])
        self.assertEqual([x["s"] for x in r["data"]], ["BTCUSDT"])
        self.assertIn("GONEUSDT", V._tk["bad"])
        # network down: ok=False, no exception
        V._tk.update(at=0.0, key=None, data=None, bad=set())

        def down(url, timeout=6):
            raise urllib.error.URLError("no route")

        r = V.fetch_tickers(["BTCUSDT"], http=down)
        self.assertFalse(r["ok"])
        self.assertIn("error", r)
        self.assertFalse(V.fetch_tickers([], http=down)["ok"])
        self.assertFalse(V.fetch_tickers(["x;rm -rf"], http=down)["ok"])  # nothing unvalidated is ever sent
        V._tk.update(at=0.0, key=None, data=None, bad=set())

    def test_ticker_route_and_living_screen_markup(self):
        d = tempfile.mkdtemp()
        fixture(d)
        srv = V.ThreadingHTTPServer(("127.0.0.1", 0), V.Handler)
        srv.dirs = {"live": d, "paper": d}
        srv.default_mode = "paper"
        srv.loopback_only = True
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        real = V.fetch_tickers
        seen = []
        V.fetch_tickers = lambda syms, **k: (seen.append(syms), {"ok": True, "data": [{"s": s, "p": "1", "c": "0"} for s in syms]})[1]
        try:
            j = json.load(urllib.request.urlopen(base + "/api/ticker?symbols=btcusdt,SOLUSDT,bad%20one"))
            self.assertTrue(j["ok"])
            self.assertEqual(seen[-1], ["BTCUSDT", "SOLUSDT"])
            req = urllib.request.Request(base + "/api/ticker", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 405)  # still read-only
            html = urllib.request.urlopen(base + "/").read().decode()
            for needle in (
                'id="hero"',
                'id="hnum"',
                "SCANNING",
                'id="rgProg"',
                'id="bg"',
                'id="fx"',
                'id="flowc"',
                'id="crt"',
                'id="ticker"',
                "rdr",
                "requestAnimationFrame",
                "prefers-reduced-motion",
                "data-api.binance.vision",
            ):
                self.assertIn(needle, html)
        finally:
            V.fetch_tickers = real
            srv.shutdown()
            srv.server_close()

    def test_no_files_written_and_no_keys_read(self):
        src = open(os.path.join(os.path.dirname(V.__file__), "visualizer.py"), encoding="utf-8").read()
        self.assertNotIn("keys.txt", src)
        self.assertNotIn("BINANCE_API", src)
        self.assertNotIn('open(path, "w"', src)


class CloseButton(unittest.TestCase):
    """The ONE write the visualizer can do: ask the desk to close (state/close_request.json). Guarded by loopback + header + origin (+ token on the LAN)."""

    class S:
        pass

    def srv(self, **kw):
        o = self.S()
        o.control_off = kw.get("off", False)
        o.control_token = kw.get("token")
        return o

    def test_control_auth_matrix(self):
        A = V.control_auth
        L = "127.0.0.1"
        H = "127.0.0.1:8765"
        self.assertEqual(A(self.srv(), L, H, None, "1", None), (True, ""))  # this PC, no token
        self.assertEqual(A(self.srv(), "::1", "[::1]:8765", "http://[::1]:8765", "1", None)[0], True)
        self.assertEqual(A(self.srv(), L, "localhost:8765", "http://localhost:8765", "1", None)[0], True)
        self.assertFalse(A(self.srv(), L, H, None, None, None)[0])  # no custom header (plain cross-site form post)
        self.assertFalse(A(self.srv(), L, H, "http://evil.example", "1", None)[0])  # cross-site origin
        self.assertFalse(A(self.srv(), L, "evil.example", None, "1", None)[0])  # DNS-rebinding Host
        self.assertFalse(A(self.srv(off=True), L, H, None, "1", None)[0])  # --no-control
        lan = "192.168.1.20"
        LH = "192.168.1.5:8765"
        self.assertFalse(A(self.srv(), lan, LH, None, "1", None)[0])  # phone, default: refused
        self.assertFalse(A(self.srv(), lan, LH, None, "1", "anything")[0])
        t = self.srv(token="s3cretTok")
        self.assertTrue(A(t, lan, LH, "http://192.168.1.5:8765", "1", "s3cretTok")[0])  # phone with the console token
        self.assertFalse(A(t, lan, LH, None, "1", "wrong")[0])
        self.assertFalse(A(t, lan, LH, None, "1", None)[0])
        self.assertFalse(A(t, lan, LH, None, None, "s3cretTok")[0])
        self.assertFalse(A(self.srv(token="x", off=True), lan, LH, None, "1", "x")[0])
        self.assertEqual(V.control_info(self.srv(), L, H), {"enabled": True, "token_required": False})
        self.assertEqual(V.control_info(self.srv(), lan, LH), {"enabled": False, "token_required": False})
        self.assertEqual(V.control_info(t, lan, LH), {"enabled": True, "token_required": True})
        self.assertFalse(V.control_info(self.srv(off=True), L, H)["enabled"])

    def _held(self, d, positions=True):
        st = json.load(open(f"{d}/status.json"))
        st["open_positions"] = [{"symbol": "SOLUSDT", "entry": 150, "stop": 138, "qty": 0.04}] if positions else []
        w(f"{d}/status.json", json.dumps(st))

    def test_request_close_writes_file_and_refuses_when_nothing_open_or_already_pending(self):
        d = tempfile.mkdtemp()
        fixture(d)
        sd = os.path.join(d, "state")
        st = lambda: json.load(open(f"{d}/status.json"))
        code, pl = V.request_close(sd, st(), {"pause_after": True})
        self.assertEqual(code, 409)
        self.assertFalse(os.path.exists(sd + "/close_request.json"))  # flat
        self._held(d)
        code, pl = V.request_close(sd, st(), {"pause_after": True, "symbols": ["solusdt", "EVILUSDT"]})
        self.assertEqual(code, 200)
        req = json.load(open(sd + "/close_request.json"))
        self.assertEqual((req["source"], req["reason"], req["pause_after"], req["symbols"]), ("visualizer", "manual", True, ["SOLUSDT"]))
        self.assertEqual(V.request_close(sd, st(), {})[0], 409)  # already waiting
        self.assertEqual([f for f in os.listdir(sd) if f.endswith(".tmp")], [])
        s = V.build_state("paper", d, "paper")
        self.assertTrue(s["close_pending"]["pause_after"])
        self.assertIsNone(s["close_result"])
        self.assertFalse(s["paused"])
        w(sd + "/PAUSE", "x")
        w(sd + "/close_result.json", json.dumps({"ts": "t", "ts_epoch": 1, "results": [{"symbol": "SOLUSDT", "status": "closed"}], "paused": True}))
        s = V.build_state("paper", d, "paper")
        self.assertTrue(s["paused"])
        self.assertEqual(s["close_result"]["results"][0]["status"], "closed")

    def test_http_post_close_end_to_end(self):
        d = tempfile.mkdtemp()
        fixture(d)
        self._held(d)
        srv = V.ThreadingHTTPServer(("127.0.0.1", 0), V.Handler)
        srv.dirs = {"live": d, "paper": d}
        srv.default_mode = "paper"
        srv.loopback_only = True
        srv.daemon_threads = True
        srv.control_off = False
        srv.control_token = None
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"

        def post(path, body=b"{}", hdr=None, host=None):
            h = {"Content-Type": "application/json"}
            h.update(hdr if hdr is not None else {"X-Desk-Control": "1"})
            if host:
                h["Host"] = host
            try:
                r = urllib.request.urlopen(urllib.request.Request(base + path, data=body, headers=h, method="POST"))
                return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read())
                except Exception:
                    return e.code, None

        try:
            self.assertTrue(json.load(urllib.request.urlopen(base + "/api/state?mode=paper"))["control"]["enabled"])
            self.assertEqual(post("/api/close", hdr={})[0], 403)
            self.assertFalse(os.path.exists(d + "/state/close_request.json"))  # no header
            self.assertEqual(post("/api/close", hdr={"X-Desk-Control": "1", "Origin": "http://evil.example"})[0], 403)
            self.assertEqual(post("/api/close", host="evil.example")[0], 403)
            self.assertEqual(post("/api/close", body=b"x" * 5000)[0], 413)
            self.assertEqual(post("/api/state")[0], 405)
            self.assertEqual(post("/api/anything")[0], 405)  # every other path stays read-only
            self.assertFalse(os.path.exists(d + "/state/close_request.json"))
            code, pl = post("/api/close", json.dumps({"mode": "paper", "pause_after": False}).encode())
            self.assertEqual((code, pl["ok"]), (200, True))
            self.assertTrue(os.path.exists(d + "/state/close_request.json"))
            self.assertEqual(post("/api/close")[0], 409)
            self.assertEqual(post("/api/close", body=b"not json")[0], 409)  # bad body = defaults; still refused as already pending
            for m in ("PUT", "DELETE"):
                with self.assertRaises(urllib.error.HTTPError) as c:
                    urllib.request.urlopen(urllib.request.Request(base + "/api/close", method=m))
                self.assertEqual(c.exception.code, 405)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_no_control_flag_and_page_has_the_button_with_confirm(self):
        d = tempfile.mkdtemp()
        fixture(d)
        self._held(d)
        srv = V.ThreadingHTTPServer(("127.0.0.1", 0), V.Handler)
        srv.dirs = {"live": d, "paper": d}
        srv.default_mode = "paper"
        srv.loopback_only = True
        srv.daemon_threads = True
        srv.control_off = True
        srv.control_token = None
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            req = urllib.request.Request(base + "/api/close", data=b"{}", headers={"X-Desk-Control": "1"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(req)
            self.assertEqual(c.exception.code, 403)
            self.assertFalse(json.load(urllib.request.urlopen(base + "/api/state?mode=paper"))["control"]["enabled"])
        finally:
            srv.shutdown()
            srv.server_close()
        html = open(os.path.join(os.path.dirname(V.__file__), "visualizer.html"), encoding="utf-8").read()
        self.assertIn("CLOSE TRADE NOW", html)
        self.assertIn("confirm(", html)
        self.assertIn("X-Desk-Control", html)


if __name__ == "__main__":
    unittest.main()

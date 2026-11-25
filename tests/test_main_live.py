"""End-to-end: main.py --mode live against the fake exchange (all network mocked): preflight -> reconcile -> scan -> buy -> stop on exchange
-> trailing replace -> target reached -> clean stop. Plus exit codes and the STOP file."""

import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as M
from desk import analyst as A
from desk import binance as B
from desk import data as DATA
from desk import filter as F
from tests.fakes import KEY, SECRET, FakeExchange

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_cfg(tmp, **over):
    s = open(os.path.join(ROOT, "config.example.toml"), encoding="utf-8").read()
    s = s.replace("[live]\nenabled = false", "[live]\nenabled = true", 1)  # live tests opt in explicitly (all network is faked)
    assert "enabled = true" in s
    s = re.sub(r"target_equity = [\d.]+", f"target_equity = {over.get('target', 12.0)}", s)
    s = re.sub(r"scan_interval_sec = \d+", "scan_interval_sec = 3600", s)
    s = re.sub(r"monitor_interval_sec = \d+", "monitor_interval_sec = 0", s)
    p = os.path.join(tmp, "config.toml")
    open(p, "w", encoding="utf-8").write(s)
    return p


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deskm")
        self.fx = FakeExchange(usdt="7.00")
        self.client = B.BinanceClient(KEY, SECRET, session=self.fx, sleep=lambda s: None)
        fx = self.fx

        def bts(_self):  # also drives the scripted price path, one step per tick
            if getattr(self, "steps", None) and fx.orders:
                m = self.steps.pop(0)
                fx.set_price("SOLUSDT", self.entry * m)
            return {x["symbol"]: x for x in fx.h_GET_api_v3_ticker_bookTicker({})}

        patches = [
            mock.patch.object(DATA.Data, "tickers_24h", lambda s: []),
            mock.patch.object(DATA.Data, "book_tickers", bts),
            mock.patch.object(DATA.Data, "book_ticker", lambda s, sym: fx.h_GET_api_v3_ticker_bookTicker({"symbol": sym})),
            mock.patch.object(DATA.Data, "klines", lambda s, *a, **k: []),
            mock.patch.object(DATA.Data, "funding_map", lambda s: ({}, {})),
            mock.patch.object(DATA.Data, "news", lambda s, *a: ([], {})),
            mock.patch("desk.watcher.Watcher.scan", lambda s: [self._cand()]),
            mock.patch.object(F.Filter, "run", lambda s, c, cy: c),
            mock.patch.object(A.Analyst, "run", lambda s, c, cy: c),
        ]
        patches.append(mock.patch("requests.get", side_effect=OSError("offline in tests")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        M.STOP = False
        self.steps = None

    def _cand(self):
        from desk.models import Candidate

        c = Candidate(symbol="SOLUSDT", base="SOL", rank=1, price=150, quote_vol_24h=1e9, change_24h_pct=1)
        c.score = 0.9
        c.factors = {}
        c.bid = 149.99
        c.ask = 150.01
        return c

    def run_main(self, **kw):
        cfgp = make_cfg(self.tmp, **kw)
        with mock.patch("desk.config.ROOT", self.tmp):  # data dir = tmp
            return M.main(["--mode", "live", "--yes", "--config", cfgp], client=self.client)

    def test_full_cycle_to_target(self):
        self.entry = float(self.fx.ask("SOLUSDT"))
        self.steps = [1.0, 1.2, 1.2, 1.8, 1.8]
        rc = self.run_main(target=12.0)
        self.assertEqual(rc, 0)
        st = json.load(open(os.path.join(self.tmp, "status.json")))
        self.assertEqual(st["state"], "target_reached")
        self.assertEqual(st["mode"], "live")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "state", "TARGET_REACHED")))
        types = [(c[1].split("/")[-1], c[2].get("type")) for c in self.fx.calls if c[0] in ("POST", "DELETE")]
        self.assertEqual(types[0], ("order", "MARKET"))
        self.assertEqual(types[1], ("order", "STOP_LOSS_LIMIT"))
        self.assertIn(("order", None), types)  # DELETE old stop when trailing
        self.assertEqual(len([c for c in self.fx.posts() if c[2]["type"] == "MARKET"]), 1)  # bought exactly once
        trades = [json.loads(l) for l in open(os.path.join(self.tmp, "logs", "trades.jsonl"))]
        self.assertEqual([t["event"] for t in trades][:2], ["entry", "stop_move"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state", "desk.pid")))
        log = open(os.path.join(self.tmp, "logs", "desk.log")).read()
        self.assertNotIn(SECRET, log)
        self.assertNotIn(KEY, log)

    def test_restart_midway_does_not_rebuy(self):
        self.entry = float(self.fx.ask("SOLUSDT"))
        self.steps = [1.0]
        M.STOP = False
        rc = self.run_main(target=1e9) if False else None
        # first run: buy then stop via STOP file after the first position tick
        cfgp = make_cfg(self.tmp, target=1e9)
        stopfile = os.path.join(self.tmp, "state", "STOP")
        real = DATA.Data.book_tickers

        def stop_after(s):
            r = real(s)
            open(stopfile, "w").write("x")
            return r

        with mock.patch("desk.config.ROOT", self.tmp), mock.patch.object(DATA.Data, "book_tickers", stop_after):
            rc = M.main(["--mode", "live", "--yes", "--config", cfgp], client=self.client)
        self.assertEqual(rc, 0)
        self.assertEqual(len([c for c in self.fx.posts() if c[2]["type"] == "MARKET"]), 1)
        os.remove(stopfile)
        M.STOP = False
        n = len(self.fx.posts())

        # second run (as after a restart/crash): must reconcile, keep the same stop and NOT buy again
        def stop2(s):
            r = real(s)
            open(stopfile, "w").write("x")
            return r

        with mock.patch("desk.config.ROOT", self.tmp), mock.patch.object(DATA.Data, "book_tickers", stop2):
            rc = M.main(["--mode", "live", "--yes", "--config", cfgp], client=self.client)
        self.assertEqual(len([c for c in self.fx.posts() if c[2]["type"] == "MARKET"]), 1)
        self.assertEqual(len(self.fx.posts()), n)

    def test_preflight_failure_exit_codes(self):
        self.fx.reject_keys = True
        self.assertEqual(self.run_main(), 2)
        self.fx.reject_keys = False
        self.fx.blocked = True
        self.assertEqual(self.run_main(), 2)
        self.assertEqual(self.fx.posts(), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state", "live_confirmed")))

    def test_second_instance_refused(self):
        os.makedirs(os.path.join(self.tmp, "state"), exist_ok=True)
        from desk.util import SingleInstance

        a = SingleInstance(os.path.join(self.tmp, "state", "desk.lock"))
        self.assertTrue(a.acquire())
        try:
            self.assertEqual(self.run_main(), 2)
        finally:
            a.release()

    def test_confirmation_required_first_time(self):
        cfgp = make_cfg(self.tmp)
        with mock.patch("desk.config.ROOT", self.tmp), mock.patch("builtins.input", return_value="yes"):
            self.assertEqual(M.main(["--mode", "live", "--config", cfgp], client=self.client), 2)
        self.assertEqual(self.fx.posts(), [])


if __name__ == "__main__":
    unittest.main()

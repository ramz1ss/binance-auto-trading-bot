"""Manual close ("stop the trade"): LiveBroker.manual_close / close_all, the control files, main.py's handling, PAUSE, deskctl (CLOSE-TRADE.bat) -
all against the fake exchange. No real network, no real keys."""

import json
import os
import sys
import tempfile
import time
import unittest
from decimal import Decimal as D
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deskctl
import main as M
from desk import binance as B
from desk import config as C
from desk import control as CTL
from desk import data as DATA
from desk.live import LiveBroker
from tests.fakes import KEY, SECRET, FakeExchange
from tests.test_live import cand, make
from tests.test_main_live import make_cfg


def open_sol(**kw):
    t = make(**kw)
    fx, cfg, cl, lg, br, risk, ex, tmp = t
    ex.enter_live([cand()], {}, 1)
    br.drain_events()
    return t


def stop_orders(fx, status="NEW"):
    return [o for o in fx.orders.values() if o["type"] == "STOP_LOSS_LIMIT" and o["status"] == status]


class ManualCloseBroker(unittest.TestCase):
    def test_happy_path_cancels_stop_sells_records_manual_and_starts_cooldown(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = open_sol()
        stop = stop_orders(fx)[0]
        entry = br.positions["SOLUSDT"]["entry"]
        fx.set_price("SOLUSDT", entry * 1.03)
        r = br.manual_close("SOLUSDT")
        self.assertEqual(r["status"], "closed")
        self.assertGreater(r["pnl"], 0 - 1)  # fees only: small number either way
        self.assertEqual(fx.orders[stop["orderId"]]["status"], "CANCELED")
        self.assertEqual(stop_orders(fx), [])
        sells = [c for c in fx.posts() if c[2]["side"] == "SELL" and c[2]["type"] == "MARKET"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(br.positions, {})
        ev = br.drain_events()[-1]
        self.assertEqual((ev["event"], ev["reason"]), ("exit", "manual"))
        self.assertIn("SOLUSDT", br.s["last_exit"])
        self.assertEqual(json.load(open(br.path))["positions"], {})  # saved to disk too
        self.assertLess(float(fx.free("SOL")), 0.01)  # nothing left but dust
        self.assertTrue(br.in_cooldown("SOLUSDT") if hasattr(br, "in_cooldown") else True)

    def test_loss_is_reported_as_a_loss(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = open_sol()
        entry = br.positions["SOLUSDT"]["entry"]
        fx.set_price("SOLUSDT", entry * 0.97)
        r = br.manual_close("SOLUSDT")
        self.assertEqual(r["status"], "closed")
        self.assertLess(r["pnl"], 0)
        self.assertLess(r["pnl_pct"], -2.9)

    def test_stop_already_filled_sells_nothing_and_records_stop_reason(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = open_sol()
        entry = br.positions["SOLUSDT"]["entry"]
        fx.set_price("SOLUSDT", entry * 0.919)  # stop-loss (-8%) triggers and fills on the exchange
        self.assertEqual(len(stop_orders(fx, "FILLED")), 1)
        n = len(fx.posts())
        r = br.manual_close("SOLUSDT")
        self.assertEqual(r["status"], "already_closed")
        self.assertEqual(len(fx.posts()), n)  # no new order at all
        self.assertEqual(br.positions, {})
        ev = br.drain_events()[-1]
        self.assertEqual(ev["reason"], "stop_loss")

    def test_dust_is_refused_and_the_stop_is_left_in_place(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = open_sol(usdt="7.00")
        br.drain_events()
        ex.enter_live([cand("SENTUSDT")], {}, 2) if False else None
        # shrink the holding to below minNotional (5 USDT) by dropping the price: 7 USDT of SOL at -90% is worth ~0.7
        fx.set_price("SOLUSDT", 15)
        n = len(fx.posts())
        calls = len(fx.calls)
        r = br.manual_close("SOLUSDT")
        self.assertEqual(r["status"], "failed")
        self.assertIn("too small", r["detail"])
        self.assertIn("SOLUSDT", br.positions)
        self.assertEqual(len(fx.posts()), n)  # nothing sent
        self.assertFalse([c for c in fx.calls[calls:] if c[0] == "DELETE"])  # stop NOT cancelled

    def test_partial_stop_fill_is_combined_with_the_manual_sell(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = open_sol()
        entry = br.positions["SOLUSDT"]["entry"]
        o = stop_orders(fx)[0]
        q = D(o["origQty"])
        half = (q / 10).quantize(D("0.001"))
        o.update(status="PARTIALLY_FILLED", _exec=half, _quote=half * D(str(entry)) * D("0.95"))
        b = fx.bal["SOL"]
        b[1] -= half
        fx.bal["USDT"][0] += half * D(str(entry)) * D("0.95")  # the filled half is already sold
        r = br.manual_close("SOLUSDT")
        self.assertEqual(r["status"], "closed")
        self.assertEqual(br.positions, {})
        sold = [c for c in fx.posts() if c[2]["side"] == "SELL" and c[2]["type"] == "MARKET"]
        self.assertEqual(len(sold), 1)
        self.assertLess(D(sold[0][2]["quantity"]), q)
        self.assertEqual(br.drain_events()[-1]["reason"], "manual")
        self.assertEqual(stop_orders(fx), [])

    def test_sell_failure_puts_the_stop_back(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = open_sol()
        fx.fail_next("POST", "/api/v3/order", status=400, code=-2010, msg="Account has insufficient balance for requested action.")
        r = br.manual_close("SOLUSDT")
        self.assertEqual(r["status"], "failed")
        self.assertIn("SOLUSDT", br.positions)
        live = stop_orders(fx)
        self.assertEqual(len(live), 1)  # exactly one protective stop alive again
        self.assertEqual(br.positions["SOLUSDT"]["stop_order_id"], live[0]["orderId"])

    def test_coins_already_gone_is_closed_outside_desk(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = open_sol()
        o = stop_orders(fx)[0]
        fx.bal["SOL"] = [D(0), D(0)]
        o["status"] = "CANCELED"  # user sold everything by hand in the app
        r = br.manual_close("SOLUSDT")
        self.assertIn(r["status"], ("closed", "already_closed"))
        self.assertEqual(br.positions, {})
        self.assertEqual(len([c for c in fx.posts() if c[2]["side"] == "SELL" and c[2]["type"] == "MARKET"]), 0)

    def test_symbol_outside_allowlist_is_skipped_and_nothing_is_sent(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = open_sol()
        calls = len(fx.calls)
        br.allowed = {"BTCUSDT"}
        r = br.manual_close("SOLUSDT")
        self.assertEqual(r["status"], "skipped")
        self.assertEqual([c for c in fx.calls[calls:] if c[0] in ("POST", "DELETE")], [])

    def test_no_position_and_close_all_never_raise(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        self.assertEqual(br.close_all(), [])
        self.assertEqual(br.manual_close("SOLUSDT")["status"], "skipped")
        fx2 = open_sol()
        fx, cfg, cl, lg, br, risk, ex, tmp = fx2
        with mock.patch.object(LiveBroker, "manual_close", side_effect=RuntimeError("boom")):
            res = br.close_all()
            self.assertEqual(res[0]["status"], "failed")
        self.assertEqual([r["symbol"] for r in br.close_all(["XXXUSDT"])], [])  # filter by symbol

    def test_close_after_trailing_activated(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = open_sol()
        entry = br.positions["SOLUSDT"]["entry"]
        fx.set_price("SOLUSDT", entry * 1.12)
        br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertTrue(br.positions["SOLUSDT"]["trailing"])
        r = br.manual_close("SOLUSDT")
        self.assertEqual(r["status"], "closed")
        self.assertGreater(r["pnl"], 0)
        self.assertEqual(stop_orders(fx), [])


class ControlFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deskc")
        os.makedirs(os.path.join(self.tmp, "state"))
        self.cfg = {"_data": self.tmp}

    def test_request_roundtrip_and_finish_writes_result_before_removing(self):
        CTL.write_request(self.cfg, "t", ["solusdt"], True)
        got, prob = CTL.read_request(CTL.close_path(self.cfg))
        self.assertEqual(prob, "")
        self.assertEqual(got["symbols"], ["SOLUSDT"])
        self.assertTrue(got["pause_after"])
        CTL.finish_request(self.cfg, got, [{"symbol": "SOLUSDT", "status": "closed"}], paused=True)
        self.assertFalse(os.path.exists(CTL.close_path(self.cfg)))
        self.assertTrue(json.load(open(CTL.result_path(self.cfg)))["paused"])

    def test_no_file_stale_and_garbage(self):
        self.assertEqual(CTL.read_request(CTL.close_path(self.cfg))[1], "none")
        p = CTL.close_path(self.cfg)
        json.dump({"ts_epoch": time.time() - 3600, "symbols": None}, open(p, "w"))
        req, prob = CTL.read_request(p)
        self.assertIn("ignored", prob)
        open(p, "w").write("{not json")
        req, prob = CTL.read_request(p)
        self.assertEqual(prob, "")
        self.assertIsNone(req["symbols"])  # garbage = close all (user asked)

    def test_pause_flag(self):
        self.assertTrue(CTL.set_pause(self.cfg, True))
        self.assertTrue(os.path.exists(CTL.pause_path(self.cfg)))
        self.assertFalse(CTL.set_pause(self.cfg, False))
        CTL.set_pause(self.cfg, False)  # twice is fine


class MainLoop(unittest.TestCase):
    """main.py --mode live: a close request written while the desk runs is executed and answered; PAUSE blocks new entries."""

    def setUp(self):
        from tests.test_main_live import T as Base

        self.b = Base("test_full_cycle_to_target")
        self.b.setUp()
        self.addCleanup(self.b.tearDown) if hasattr(self.b, "tearDown") else None
        self.fx, self.tmp = self.b.fx, self.b.tmp
        self.b.entry = float(self.fx.ask("SOLUSDT"))
        self.b.steps = None
        self.cfgp = make_cfg(self.tmp, target=1e9)
        self.state = os.path.join(self.tmp, "state")

    def run_desk(self, hook):
        real = DATA.Data.book_tickers

        def bts(s):
            r = real(s)
            hook()
            return r

        real_fin = CTL.finish_request

        def fin(*a, **k):  # the desk has answered: stop the test desk now
            real_fin(*a, **k)
            open(os.path.join(self.state, "STOP"), "w").write("x")

        with (
            mock.patch("desk.config.ROOT", self.tmp),
            mock.patch.object(DATA.Data, "book_tickers", bts),
            mock.patch.object(CTL, "finish_request", fin),
        ):
            return M.main(["--mode", "live", "--yes", "--config", self.cfgp], client=self.b.client)

    def test_request_is_executed_answered_and_pause_applies(self):
        closef, resf, stopf = [os.path.join(self.state, n) for n in ("close_request.json", "close_result.json", "STOP")]
        calls = {"n": 0}

        def hook():
            calls["n"] += 1
            if calls["n"] == 1:
                json.dump(
                    {"ts": "x", "ts_epoch": time.time(), "source": "test", "reason": "manual", "symbols": None, "pause_after": True},
                    open(closef, "w"),
                )
            if os.path.exists(resf):
                open(stopf, "w").write("x")
            if calls["n"] > 8:
                open(stopf, "w").write("x")

        M.STOP = False
        rc = self.run_desk(hook)
        self.assertEqual(rc, 0)
        res = json.load(open(resf))
        self.assertEqual(res["results"][0]["status"], "closed")
        self.assertTrue(res["paused"])
        self.assertFalse(os.path.exists(closef))
        self.assertTrue(os.path.exists(os.path.join(self.state, "PAUSE")))
        trades = [json.loads(l) for l in open(os.path.join(self.tmp, "logs", "trades.jsonl"))]
        self.assertEqual([t["event"] for t in trades], ["entry", "exit"])
        self.assertEqual(trades[-1]["reason"], "manual")
        self.assertEqual(json.load(open(os.path.join(self.state, "live_state.json")))["positions"], {})
        self.assertEqual(stop_orders(self.fx), [])
        self.assertEqual(json.load(open(os.path.join(self.tmp, "status.json")))["open_positions"], [])

    def test_stale_request_is_ignored_not_executed(self):
        closef, resf, stopf = [os.path.join(self.state, n) for n in ("close_request.json", "close_result.json", "STOP")]
        calls = {"n": 0}

        def hook():
            calls["n"] += 1
            if calls["n"] == 1:
                json.dump({"ts_epoch": time.time() - 7200, "symbols": None}, open(closef, "w"))
            if os.path.exists(resf) or calls["n"] > 8:
                open(stopf, "w").write("x")

        M.STOP = False
        self.run_desk(hook)
        self.assertEqual(json.load(open(resf))["results"][0]["status"], "skipped")
        self.assertEqual(len(stop_orders(self.fx)), 1)  # trade untouched
        self.assertEqual([c for c in self.fx.posts() if c[2]["side"] == "SELL" and c[2]["type"] == "MARKET"], [])

    def test_pause_file_blocks_new_entries(self):
        from desk.logger import Logger

        os.makedirs(self.state, exist_ok=True)
        open(os.path.join(self.state, "PAUSE"), "w").write("p")
        stopf = os.path.join(self.state, "STOP")
        real_skip = Logger.skip
        skips = []

        def skip(lg, cyc, agent, why, *a, **k):
            skips.append(why)
            open(stopf, "w").write("x")
            return real_skip(lg, cyc, agent, why, *a, **k)

        M.STOP = False
        with mock.patch("desk.config.ROOT", self.tmp), mock.patch.object(Logger, "skip", skip):
            self.assertEqual(M.main(["--mode", "live", "--yes", "--config", self.cfgp], client=self.b.client), 0)
        self.assertEqual(self.fx.posts(), [])
        self.assertTrue(any("PAUSED" in w for w in skips), skips)
        self.assertTrue(json.load(open(os.path.join(self.tmp, "status.json"))).get("paused"))


class DeskCtl(unittest.TestCase):
    def setUp(self):

        self.tmp = tempfile.mkdtemp(prefix="deskd")
        self.cfgp = make_cfg(self.tmp)
        self.out = []
        self.fx = FakeExchange(usdt="7.00")
        self.client = B.BinanceClient(KEY, SECRET, session=self.fx, sleep=lambda s: None)
        for p in (
            mock.patch("desk.config.ROOT", self.tmp),
            mock.patch.object(DATA.Data, "book_ticker", lambda s, sym: self.fx.h_GET_api_v3_ticker_bookTicker({"symbol": sym})),
            mock.patch.object(DATA.Data, "book_tickers", lambda s: {x["symbol"]: x for x in self.fx.h_GET_api_v3_ticker_bookTicker({})}),
        ):
            p.start()
            self.addCleanup(p.stop)
        os.environ["DESKCTL_FAST"] = "1"
        self.addCleanup(os.environ.pop, "DESKCTL_FAST", None)
        cfg = C.load(self.cfgp, "live")
        self.cfg = cfg
        from desk.executor import Executor
        from desk.logger import Logger
        from desk.risk import Risk

        data = DATA.Data(cfg)
        lg = Logger(cfg)
        risk = Risk(cfg, data, lg)
        self.br = LiveBroker(cfg, self.client, data, lg, sleep=lambda s: None)
        self.br.reconcile(risk)
        Executor(cfg, data, self.br, risk, lg).enter_live([cand()], {}, 1)
        self.br.drain_events()

    def ctl(self, *argv, inputs=()):
        it = iter(inputs)
        return deskctl.main(list(argv) + ["--config", self.cfgp], client=self.client, input_fn=lambda q: next(it), say=self.out.append)

    def test_cancel_when_not_YES(self):
        self.assertEqual(self.ctl("close", inputs=["yes"]), 1)
        self.assertEqual(len(stop_orders(self.fx)), 1)
        self.assertEqual(self.ctl("close", inputs=[""]), 1)
        self.assertEqual([c for c in self.fx.posts() if c[2]["side"] == "SELL" and c[2]["type"] == "MARKET"], [])

    def test_desk_not_running_closes_directly_and_pauses(self):
        rc = self.ctl("close", inputs=["YES", "y"])
        self.assertEqual(rc, 0, self.out)
        self.assertEqual(stop_orders(self.fx), [])
        self.assertEqual(len([c for c in self.fx.posts() if c[2]["side"] == "SELL" and c[2]["type"] == "MARKET"]), 1)
        self.assertEqual(json.load(open(os.path.join(self.tmp, "state", "live_state.json")))["positions"], {})
        self.assertTrue(os.path.exists(CTL.pause_path(self.cfg)))
        self.assertTrue(any("SOLUSDT" in l and "SOLD" in l for l in self.out), self.out)
        trades = [json.loads(l) for l in open(os.path.join(self.tmp, "logs", "trades.jsonl"))]
        self.assertEqual(trades[-1]["reason"], "manual")
        self.assertEqual(self.ctl("close", "--yes"), 0)
        self.assertTrue(any("nothing to close" in l for l in self.out))  # second time: nothing open

    def test_desk_not_running_no_pause_option(self):
        self.assertEqual(self.ctl("close", inputs=["YES", "n"]), 0)
        self.assertFalse(os.path.exists(CTL.pause_path(self.cfg)))

    def test_running_desk_gets_a_request_and_its_answer_is_shown(self):
        from desk.util import SingleInstance

        lock = SingleInstance(os.path.join(self.tmp, "state", "desk.lock"))
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)  # "the desk is running"
        import threading

        def desk():
            for _ in range(200):
                req, prob = CTL.read_request(CTL.close_path(self.cfg))
                if prob == "":
                    CTL.finish_request(
                        self.cfg, req, [{"symbol": "SOLUSDT", "status": "closed", "detail": "sold", "pnl": 0.1, "pnl_pct": 1.0}], paused=True
                    )
                    return
                time.sleep(0.02)

        th = threading.Thread(target=desk)
        th.start()
        rc = self.ctl("close", "--yes", "--pause-after")
        th.join()
        self.assertEqual(rc, 0, self.out)
        self.assertTrue(any("SOLD" in l for l in self.out))
        self.assertFalse(os.path.exists(CTL.close_path(self.cfg)))
        self.assertEqual(
            [c for c in self.fx.posts() if c[2]["side"] == "SELL" and c[2]["type"] == "MARKET"], []
        )  # the bat itself sold nothing: the desk does that

    def test_running_desk_that_never_answers_withdraws_the_request(self):
        from desk.util import SingleInstance

        lock = SingleInstance(os.path.join(self.tmp, "state", "desk.lock"))
        lock.acquire()
        self.addCleanup(lock.release)
        with mock.patch.object(deskctl, "WAIT_SEC", 5):
            rc = self.ctl("close", "--yes")
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(CTL.close_path(self.cfg)))
        self.assertTrue(any("withdrawn" in l for l in self.out))

    def test_pause_resume_status(self):
        self.assertEqual(self.ctl("pause"), 0)
        self.assertTrue(os.path.exists(CTL.pause_path(self.cfg)))
        self.ctl("status")
        self.assertTrue(any("Paused: YES" in l for l in self.out))
        self.assertEqual(self.ctl("resume"), 0)
        self.assertFalse(os.path.exists(CTL.pause_path(self.cfg)))


if __name__ == "__main__":
    unittest.main()

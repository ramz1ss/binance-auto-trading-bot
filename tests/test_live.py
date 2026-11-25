import json
import logging
import os
import sys
import tempfile
import time
import unittest
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from desk import binance as B
from desk import config as C
from desk import keys as K
from desk import preflight
from desk.executor import Executor
from desk.live import LiveBroker
from desk.models import Candidate
from desk.risk import Risk
from tests.fakes import KEY, SECRET, FakeExchange


class NullLog:
    def __init__(self):
        self.log = logging.getLogger("t")
        self.skips = []
        self.trades = []

    def skip(self, *a, **k):
        self.skips.append(a)

    def trade(self, ev):
        self.trades.append(ev)

    def kill(self, *a, **k):
        pass


class StubData:  # price data used by Executor/Risk
    def __init__(self, fx):
        self.fx = fx

    def book_ticker(self, s):
        return self.fx.h_GET_api_v3_ticker_bookTicker({"symbol": s})

    def klines(self, *a, **k):
        return []


def make(fx=None, usdt="7.00", mode_over=None):
    fx = fx or FakeExchange(usdt=usdt)
    tmp = tempfile.mkdtemp(prefix="deskt")
    cfg = C.load(mode="live")
    cfg["_data"] = tmp
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    cfg["live"].update(mode_over or {})
    cl = B.BinanceClient(KEY, SECRET, session=fx, sleep=lambda s: None)
    lg = NullLog()
    data = StubData(fx)
    risk = Risk(cfg, data, lg)
    br = LiveBroker(cfg, cl, data, lg, sleep=lambda s: None)
    br.reconcile(risk)
    ex = Executor(cfg, data, br, risk, lg)
    return fx, cfg, cl, lg, br, risk, ex, tmp


def cand(sym="SOLUSDT", score=0.8):
    c = Candidate(symbol=sym, base=sym[:-4], rank=1, price=0, quote_vol_24h=0, change_24h_pct=0)
    c.score = score
    return c


class T(unittest.TestCase):
    # ------------------------------------------------------------------ signing / formatting
    def test_signature_matches_binance_docs_vector(self):
        # Public example key/secret and signature from the Binance Spot API documentation (not a real credential).
        cl = B.BinanceClient("k", "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j", recv_window=5000)
        real = time.time
        time.time = lambda: 1499827319.559
        try:
            q = cl._sign({"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": 1, "price": "0.1"})
        finally:
            time.time = real
        self.assertTrue(q.endswith("&signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"), q)

    def test_decimal_formatting_and_rounding(self):
        self.assertEqual(B.fmt("0.00001230"), "0.0000123")
        self.assertEqual(B.fmt(D("1E+2")), "100")
        self.assertEqual(B.fmt(1e-7), "0.0000001")
        self.assertEqual(B.floor_to("1.2399", "0.01"), D("1.23"))
        self.assertEqual(B.floor_to("0.99999", "0.001"), D("0.999"))
        self.assertEqual(B.fmt(B.floor_to("123456.789", "1")), "123456")

    def test_symbol_rules_from_exchange_info(self):
        fx = FakeExchange()
        R = B.SymbolRules(fx.h_GET_api_v3_exchangeInfo({"symbol": "BTCUSDT"})["symbols"][0])
        self.assertEqual((R.step, R.tick, R.min_notional), (D("0.00001"), D("0.01"), D("5")))
        self.assertEqual(R.qty_down("0.000129999"), D("0.00012"))
        self.assertEqual(R.price_down("82800.019"), D("82800.01"))
        self.assertIsNone(R.tradable_problem())

    def test_order_param_construction(self):
        self.assertEqual(
            B.market_buy_params("BTCUSDT", D("6.86"), "c1"),
            {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": "6.86", "newClientOrderId": "c1", "newOrderRespType": "FULL"},
        )
        p = B.stop_limit_params("BTCUSDT", D("0.00007"), D("82800.00"), D("81972.00"), "s1")
        self.assertEqual(
            (p["type"], p["side"], p["timeInForce"], p["quantity"], p["stopPrice"], p["price"]),
            ("STOP_LOSS_LIMIT", "SELL", "GTC", "0.00007", "82800", "81972"),
        )
        o = B.oco_params("BTCUSDT", D("0.00007"), D("82800"), D("81972"), D("270000"), "s", "t", "l")
        self.assertEqual(
            (o["aboveType"], o["abovePrice"], o["belowType"], o["belowStopPrice"], o["belowPrice"], o["belowTimeInForce"], o["side"]),
            ("LIMIT_MAKER", "270000", "STOP_LOSS_LIMIT", "82800", "81972", "GTC", "SELL"),
        )
        self.assertNotIn("price", o)
        self.assertNotIn("stopPrice", o)

    # ------------------------------------------------------------------ sizing
    def test_plan_live_sizing(self):
        fx, cfg, cl, lg, br, risk, ex, _ = make()
        R = br.rules("SOLUSDT")
        ok, why, q = ex.plan_live(R, "6.86", "150")
        self.assertTrue(ok, why)
        self.assertEqual(q, (D("0.044"), D("0.045")))  # fill 0.045, fee in SOL costs one lot step -> sell 0.044
        ok, why, _ = ex.plan_live(R, "4.99", "150")
        self.assertFalse(ok)
        self.assertIn("minimum", why)
        ok, why, _ = ex.plan_live(R, "5.30", "150")
        self.assertFalse(ok)
        self.assertIn("protective stop", why)  # entry fits but its stop would not
        ok, why, _ = ex.plan_live(R, "6.86", "1000000")
        self.assertFalse(ok)  # below min lot
        # BTC at $90k with $7 cannot be protected: lot step 0.00001 (= $0.90) eats the fee
        ok, why, _ = ex.plan_live(B.SymbolRules(FakeExchange().h_GET_api_v3_exchangeInfo({"symbol": "BTCUSDT"})["symbols"][0]), "6.86", "90000")
        self.assertFalse(ok)
        self.assertIn("protective stop", why)

    def test_small_balance_uses_98_percent_of_free_usdt(self):
        fx, cfg, cl, lg, br, risk, ex, _ = make(usdt="7.00")
        ev = ex.enter_live([cand()], {}, 1)
        self.assertEqual(fx.posts()[0][2]["quoteOrderQty"], "6.86")  # floor(7*0.98, 0.01)
        self.assertEqual(ev["event"], "entry")

    def test_large_balance_caps_at_10_percent(self):
        fx, cfg, cl, lg, br, risk, ex, _ = make(usdt="1000")
        ex.enter_live([cand()], {}, 1)
        self.assertEqual(fx.posts()[0][2]["quoteOrderQty"], "100")

    # ------------------------------------------------------------------ entry + stop
    def test_buy_places_minus8_stop_on_exchange_after_fill(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        ev = ex.enter_live([cand()], {}, 1)
        pos = br.positions["SOLUSDT"]
        orders = fx.posts()
        self.assertEqual([o[2]["type"] for o in orders], ["MARKET", "STOP_LOSS_LIMIT"])
        stop = orders[1][2]
        entry = float(fx.ask("SOLUSDT"))
        self.assertAlmostEqual(float(stop["stopPrice"]), entry * 0.92, delta=0.011)
        self.assertAlmostEqual(float(stop["price"]), entry * 0.92 * 0.99, delta=0.011)
        # quantity sold = bought qty minus the 0.1% fee paid in BTC, rounded down to the lot step
        self.assertEqual(stop["quantity"], "0.044")  # 0.045 bought, 0.1% fee paid in SOL -> 0.044955 -> floor to the 0.001 step
        self.assertEqual(orders[0][2]["quoteOrderQty"], "6.86")
        self.assertTrue(pos["stop_order_id"])
        self.assertEqual(ev["stop"], pos["stop"])
        saved = json.load(open(os.path.join(tmp, "state", "live_state.json")))
        self.assertIn("SOLUSDT", saved["positions"])
        self.assertIsNone(saved["pending_entry"])

    def test_stop_failure_after_fill_sells_immediately(self):
        fx, cfg, cl, lg, br, risk, ex, _ = make()
        orig = fx.h_POST_api_v3_order

        def patched(p):
            if p["type"] == "STOP_LOSS_LIMIT":
                return fx._err(400, -1013, "Filter failure: NOTIONAL")
            return orig(p)

        fx.h_POST_api_v3_order = patched
        ex.enter_live([cand()], {}, 1)
        self.assertEqual(br.positions, {})
        evs = br.drain_events()
        self.assertEqual(evs[-1]["event"], "exit")
        self.assertEqual(evs[-1]["reason"], "stop_failed_emergency_exit")
        self.assertEqual(fx.free("SOL"), fx.total("SOL"))  # nothing locked
        self.assertLess(fx.free("SOL") * fx.px["SOLUSDT"], D("0.7"))  # only fee-dust left

    def test_no_entry_when_stop_could_not_be_protected(self):
        fx, cfg, cl, lg, br, risk, ex, _ = make(usdt="5.60")  # 98% = 5.48 -> stop leg would be < 5
        self.assertIsNone(ex.enter_live([cand()], {}, 1))
        self.assertEqual(fx.posts(), [])
        self.assertTrue(any("protective stop" in s[2] for s in lg.skips))

    def test_tiny_price_coin_rounding(self):
        fx, cfg, cl, lg, br, risk, ex, _ = make(usdt="20")
        ev = ex.enter_live([cand("PEPEUSDT")], {}, 1)
        self.assertEqual(ev["event"], "entry")
        stop = fx.posts()[1][2]
        self.assertEqual(D(stop["stopPrice"]) % D("0.00000001"), 0)
        self.assertEqual(D(stop["quantity"]) % 1, 0)

    # ------------------------------------------------------------------ trailing
    def _open(self, **kw):
        t = make(**kw)
        fx, cfg, cl, lg, br, risk, ex, tmp = t
        ex.enter_live([cand()], {}, 1)
        br.drain_events()
        return t

    def test_trailing_not_active_below_10_percent(self):
        fx, cfg, cl, lg, br, risk, ex, _ = self._open()
        self.assertEqual(cfg["risk"]["trail_activate_pct"], 10.0)
        n = len(fx.posts())
        entry = br.positions["SOLUSDT"]["entry"]
        fx.set_price("SOLUSDT", entry * 1.09)
        br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertEqual(len(fx.posts()), n)
        self.assertFalse(br.positions["SOLUSDT"]["trailing"])

    def test_trailing_activates_at_10_percent(self):
        fx, cfg, cl, lg, br, risk, ex, _ = self._open()
        entry = br.positions["SOLUSDT"]["entry"]
        fx.set_price("SOLUSDT", entry * 1.11)
        evs = br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertTrue(br.positions["SOLUSDT"]["trailing"])
        self.assertEqual(evs[0]["event"], "stop_move")

    def test_trailing_replaces_stop_at_5pct_below_peak_and_only_ratchets_up(self):
        fx, cfg, cl, lg, br, risk, ex, _ = self._open()
        pos = br.positions["SOLUSDT"]
        entry = pos["entry"]
        first_id = pos["stop_order_id"]
        s0 = pos["stop"]
        fx.set_price("SOLUSDT", entry * 1.20)
        bid = float(fx.bid("SOLUSDT"))
        evs = br.manage({"SOLUSDT": bid}, risk)
        pos = br.positions["SOLUSDT"]
        self.assertTrue(pos["trailing"])
        self.assertNotEqual(pos["stop_order_id"], first_id)
        self.assertEqual(fx.orders[first_id]["status"], "CANCELED")
        self.assertAlmostEqual(pos["stop"], bid * 0.95, delta=0.011)
        self.assertGreater(pos["stop"], s0)
        live = [o for o in fx.orders.values() if o["status"] == "NEW"]
        self.assertEqual(len(live), 1)  # exactly one stop alive
        self.assertEqual(D(live[0]["stopPrice"]), D(str(pos["stop"])))
        self.assertEqual(evs[0]["event"], "stop_move")
        # price drifts back down but stays above the stop: no change
        st = pos["stop"]
        n = len(fx.posts())
        fx.set_price("SOLUSDT", entry * 1.17)
        br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertEqual(br.positions["SOLUSDT"]["stop"], st)
        self.assertEqual(len(fx.posts()), n)
        # tiny new high (< min step) does not churn orders
        fx.set_price("SOLUSDT", entry * 1.2001)
        br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertEqual(len(fx.posts()), n)
        # new real high: second replace
        fx.set_price("SOLUSDT", entry * 1.30)
        br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertGreater(br.positions["SOLUSDT"]["stop"], st)
        self.assertEqual(len([o for o in fx.orders.values() if o["status"] == "NEW"]), 1)

    def test_trailing_stop_fill_is_recorded(self):
        fx, cfg, cl, lg, br, risk, ex, _ = self._open()
        entry = br.positions["SOLUSDT"]["entry"]
        fx.set_price("SOLUSDT", entry * 1.25)
        br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        stop = br.positions["SOLUSDT"]["stop"]
        br.drain_events()
        fx.set_price("SOLUSDT", stop * 0.999)  # drops through the trailing stop, limit still fillable
        evs = br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        ex_ev = [e for e in evs if e["event"] == "exit"][0]
        self.assertEqual(ex_ev["reason"], "trailing_stop")
        self.assertGreater(ex_ev["pnl"], 0)
        self.assertEqual(br.positions, {})
        self.assertEqual((br.s["closed"], br.s["wins"]), (1, 1))
        self.assertIn("SOLUSDT", br.s["last_exit"])

    def test_initial_stop_loss_fill_recorded_as_loss(self):
        fx, cfg, cl, lg, br, risk, ex, _ = self._open()
        stop = br.positions["SOLUSDT"]["stop"]
        fx.set_price("SOLUSDT", stop * 0.998)
        evs = br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        e = [x for x in evs if x["event"] == "exit"][0]
        self.assertEqual(e["reason"], "stop_loss")
        self.assertLess(e["pnl_pct"], -7.0)
        self.assertGreater(e["pnl_pct"], -11.0)  # -8% stop + fees + ~2% lot-step dust at this tiny size
        self.assertGreater(br.cash, 5.0)

    def test_gap_through_stop_limit_triggers_market_exit(self):
        fx, cfg, cl, lg, br, risk, ex, _ = self._open()
        pos = br.positions["SOLUSDT"]
        fx.set_price("SOLUSDT", pos["stop_limit"] * 0.9)  # far below the limit: limit order cannot fill
        self.assertEqual(fx.orders[pos["stop_order_id"]]["status"], "NEW")
        evs = br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertEqual([e["reason"] for e in evs if e["event"] == "exit"], ["stop_gap_market_exit"])
        self.assertEqual(br.positions, {})
        self.assertEqual(len([o for o in fx.orders.values() if o["status"] == "NEW"]), 0)

    def test_replace_stop_would_trigger_sells_at_market(self):
        fx, cfg, cl, lg, br, risk, ex, _ = self._open()
        pos = br.positions["SOLUSDT"]
        entry = pos["entry"]
        pos["peak"] = entry * 1.5
        pos["trailing"] = True  # pretend peak was high, price now far below new stop
        br._replace_stop(pos, entry * 5)  # stop above market -> exchange says would trigger
        self.assertEqual(br.positions, {})
        self.assertEqual(br.drain_events()[-1]["reason"], "trailing_stop_market_exit")

    # ------------------------------------------------------------------ OCO mode
    def test_oco_mode_entry_and_trailing(self):
        fx, cfg, cl, lg, br, risk, ex, _ = make(mode_over={"stop_mode": "oco"})
        ex.enter_live([cand()], {}, 1)
        pos = br.positions["SOLUSDT"]
        br.drain_events()
        o = fx.posts("/api/v3/orderList/oco")[0][2]
        self.assertEqual((o["aboveType"], o["belowType"], o["side"]), ("LIMIT_MAKER", "STOP_LOSS_LIMIT", "SELL"))
        self.assertGreater(D(o["abovePrice"]), D(o["belowStopPrice"]) * 3)
        self.assertIsNotNone(pos["order_list_id"])
        entry = pos["entry"]
        fx.set_price("SOLUSDT", entry * 1.25)
        br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertEqual(len(fx.posts("/api/v3/orderList/oco")), 2)
        self.assertEqual(len([x for x in fx.orders.values() if x["status"] == "NEW"]), 2)  # one OCO (2 legs) alive
        self.assertAlmostEqual(br.positions["SOLUSDT"]["stop"], float(fx.bid("SOLUSDT")) * 0.95, delta=0.011)
        stop = br.positions["SOLUSDT"]["stop"]
        fx.set_price("SOLUSDT", stop * 0.999)
        evs = br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertEqual([e["reason"] for e in evs if e["event"] == "exit"], ["trailing_stop"])

    # ------------------------------------------------------------------ reconciliation
    def test_restart_with_open_position_does_not_rebuy_or_touch_stop(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = self._open()
        n = len(fx.posts())
        sid = br.positions["SOLUSDT"]["stop_order_id"]
        br2 = LiveBroker(cfg, cl, StubData(fx), lg, sleep=lambda s: None)
        rep = br2.reconcile(risk)
        ex2 = Executor(cfg, StubData(fx), br2, risk, lg)
        self.assertEqual(br2.positions["SOLUSDT"]["stop_order_id"], sid)
        self.assertEqual(len(fx.posts()), n)
        self.assertIsNone(ex2.enter_live([cand("SOLUSDT")], {}, 2))
        self.assertEqual(len(fx.posts()), n)  # one position at a time under $50
        self.assertTrue(any("still protected" in r for r in rep))

    def test_restart_after_state_file_lost_adopts_desk_stop(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = self._open()
        os.remove(os.path.join(tmp, "state", "live_state.json"))
        n = len(fx.posts())
        br2 = LiveBroker(cfg, cl, StubData(fx), lg, sleep=lambda s: None)
        br2.reconcile(risk)
        p = br2.positions["SOLUSDT"]
        self.assertEqual(p["stop_order_id"], br.positions["SOLUSDT"]["stop_order_id"])
        self.assertAlmostEqual(p["entry"], br.positions["SOLUSDT"]["entry"], places=2)
        self.assertEqual(len(fx.posts()), n)
        self.assertIsNone(Executor(cfg, StubData(fx), br2, risk, lg).enter_live([cand("SOLUSDT")], {}, 2))

    def test_restart_after_crash_between_fill_and_stop(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        orig = fx.h_POST_api_v3_order

        def boom(p):
            if p["type"] == "STOP_LOSS_LIMIT":
                raise KeyboardInterrupt("simulated power cut")
            return orig(p)

        fx.h_POST_api_v3_order = boom
        with self.assertRaises(KeyboardInterrupt):
            ex.enter_live([cand()], {}, 1)
        fx.h_POST_api_v3_order = orig
        st = json.load(open(os.path.join(tmp, "state", "live_state.json")))
        self.assertIn("SOLUSDT", st["positions"])
        self.assertIsNone(st["positions"]["SOLUSDT"]["stop_order_id"])  # recorded, but unprotected
        br2 = LiveBroker(cfg, cl, StubData(fx), lg, sleep=lambda s: None)
        rep = br2.reconcile(risk)
        self.assertTrue(br2.positions["SOLUSDT"]["stop_order_id"])
        self.assertTrue(any("missing" in r for r in rep))
        self.assertEqual(len([o for o in fx.orders.values() if o["status"] == "NEW"]), 1)
        self.assertEqual(len([p for p in fx.posts() if p[2]["type"] == "MARKET"]), 1)

    def test_restart_with_pending_entry_that_filled(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        orig = fx.h_POST_api_v3_order

        def die_after_buy(p):
            r = orig(p)
            if p["type"] == "MARKET" and p["side"] == "BUY":
                raise KeyboardInterrupt("crash right after the buy")
            return r

        fx.h_POST_api_v3_order = die_after_buy
        with self.assertRaises(KeyboardInterrupt):
            ex.enter_live([cand()], {}, 1)
        fx.h_POST_api_v3_order = orig
        st = json.load(open(os.path.join(tmp, "state", "live_state.json")))
        self.assertIsNotNone(st["pending_entry"])
        self.assertEqual(st["positions"], {})
        br2 = LiveBroker(cfg, cl, StubData(fx), lg, sleep=lambda s: None)
        br2.reconcile(risk)
        self.assertIn("SOLUSDT", br2.positions)
        self.assertTrue(br2.positions["SOLUSDT"]["stop_order_id"])
        self.assertIsNone(br2.s["pending_entry"])
        self.assertEqual(len([p for p in fx.posts() if p[2]["type"] == "MARKET"]), 1)

    def test_restart_when_stop_filled_while_off(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = self._open()
        fx.set_price("SOLUSDT", br.positions["SOLUSDT"]["stop"] * 0.999)
        br2 = LiveBroker(cfg, cl, StubData(fx), lg, sleep=lambda s: None)
        br2.reconcile(risk)
        self.assertEqual(br2.positions, {})
        self.assertEqual(br2.s["closed"], 1)
        self.assertEqual(br2.drain_events()[0]["reason"], "stop_filled_while_desk_was_off")

    def test_stop_cancelled_by_user_is_replaced(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = self._open()
        fx._cancel(fx.orders[br.positions["SOLUSDT"]["stop_order_id"]])
        br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertEqual(len([o for o in fx.orders.values() if o["status"] == "NEW"]), 1)

    def test_lost_stop_id_relinks_instead_of_double_placing(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = self._open()
        sid = br.positions["SOLUSDT"]["stop_order_id"]
        br.positions["SOLUSDT"]["stop_order_id"] = None
        n = len(fx.posts())
        br.manage({"SOLUSDT": float(fx.bid("SOLUSDT"))}, risk)
        self.assertEqual(br.positions["SOLUSDT"]["stop_order_id"], sid)
        self.assertEqual(len(fx.posts()), n)

    def test_foreign_orders_and_coins_are_ignored(self):
        fx = FakeExchange(usdt="7.00")
        fx.bal["SOL"] = [D("10"), D(0)]
        fx.orders[1] = {
            "symbol": "SOLUSDT",
            "orderId": 1,
            "orderListId": -1,
            "clientOrderId": "myownorder",
            "side": "SELL",
            "type": "STOP_LOSS_LIMIT",
            "status": "NEW",
            "_exec": D(0),
            "_quote": D(0),
            "origQty": "5",
            "price": "100",
            "stopPrice": "110",
        }
        fx, cfg, cl, lg, br, risk, ex, tmp = make(fx)
        self.assertEqual(br.positions, {})
        self.assertEqual([f["asset"] for f in br.foreign], ["SOL"])
        self.assertAlmostEqual(br.equity({}), 7.0)  # foreign SOL is not counted

    def test_unclear_buy_response_never_double_buys(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        fx.drop_next_response("POST", "/api/v3/order")  # buy executes, response lost (timeout)
        ev = ex.enter_live([cand()], {}, 1)
        self.assertEqual(ev["event"], "entry")
        self.assertEqual(len([p for p in fx.posts() if p[2]["type"] == "MARKET"]), 1)
        self.assertTrue(br.positions["SOLUSDT"]["stop_order_id"])

    def test_buy_503_not_executed_then_blocks_until_reconciled(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        fx.fail_next("POST", "/api/v3/order", 503, -1007)  # nothing executed
        self.assertIsNone(ex.enter_live([cand()], {}, 1))
        self.assertEqual(br.positions, {})
        self.assertTrue(br.unresolved)  # buying is blocked until the account is re-checked
        ev = ex.enter_live([cand()], {}, 2)  # next cycle: reconcile finds the order never existed, clears it, buys once
        self.assertEqual(ev["event"], "entry")
        self.assertEqual(len([p for p in fx.posts() if p[2]["type"] == "MARKET"]), 1)

    def test_clock_skew_is_corrected(self):
        fx = FakeExchange()
        fx.clock_skew_ms = -30000  # PC clock 30 s ahead of Binance
        cl = B.BinanceClient(KEY, SECRET, session=fx, sleep=lambda s: None)
        acc = cl.account()  # first try -1021 -> resync -> retry
        self.assertTrue(acc["canTrade"])
        self.assertLess(abs(cl.time_offset_ms + 30000), 500)

    # ------------------------------------------------------------------ preflight
    def _pf(self, fx, cfg=None, **kw):
        cfg = cfg or C.load(mode="live")
        cfg["_data"] = tempfile.mkdtemp()
        os.makedirs(os.path.join(cfg["_data"], "state"))
        cfg["data"]["binance_futures_base"] = ""
        lines = []
        cl = B.BinanceClient(KEY, SECRET, session=fx, sleep=lambda s: None)
        r = preflight.run(cfg, client=cl, say=lines.append)
        return r, "\n".join(lines)

    def test_preflight_ok(self):
        r, out = self._pf(FakeExchange(usdt="7.0"))
        self.assertTrue(r.ok, out)
        for s in ("Reached api.binance.com", "canTrade = true", "Withdrawals are disabled", "7.0000"):
            self.assertIn(s, out)
        self.assertNotIn(SECRET, out)
        self.assertNotIn(KEY, out)

    def test_preflight_refuses_withdrawal_enabled_keys_by_default(self):
        r, out = self._pf(FakeExchange(withdrawals=True))
        self.assertFalse(r.ok)
        self.assertIn("WITHDRAWALS ARE ENABLED", out)

    def test_preflight_warns_on_withdrawals_and_no_ip_restriction_but_passes_when_accepted(self):
        cfg = C.load(mode="live")
        cfg["live"]["allow_withdrawal_enabled_key"] = True
        r, out = self._pf(FakeExchange(withdrawals=True, ip_restrict=False), cfg)
        self.assertTrue(r.ok)
        self.assertIn("[WARN] WITHDRAWALS ARE ENABLED", out)
        self.assertIn("no IP restriction", out)

    def test_preflight_refusals(self):
        fx = FakeExchange()
        fx.blocked = True
        r, out = self._pf(fx)
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "permanent")
        self.assertIn("451", out)
        fx = FakeExchange()
        fx.reject_keys = True
        r, out = self._pf(fx)
        self.assertFalse(r.ok)
        self.assertIn("rejected the API key", out)
        r, out = self._pf(FakeExchange(can_trade=False))
        self.assertFalse(r.ok)
        self.assertIn("cannot trade", out)
        r, out = self._pf(FakeExchange(spot_trading=False))
        self.assertFalse(r.ok)
        self.assertIn("Spot & Margin Trading", out)
        r, out = self._pf(FakeExchange(usdt="3.00"))
        self.assertFalse(r.ok)
        self.assertIn("at least 6.00", out)

    def test_preflight_network_down_is_transient(self):
        import requests

        class Down:
            def request(self, *a, **k):
                raise requests.ConnectionError("no route")

        cfg = C.load(mode="live")
        cfg["_data"] = tempfile.mkdtemp()
        os.makedirs(os.path.join(cfg["_data"], "state"))
        cl = B.BinanceClient(KEY, SECRET, session=Down(), sleep=lambda s: None)
        r = preflight.run(cfg, client=cl, say=lambda s: None)
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "transient")

    def test_preflight_missing_or_placeholder_keys(self):
        d = tempfile.mkdtemp()
        cfg = C.load(mode="live")
        cfg["_root"] = d
        cfg["_data"] = d
        os.environ.pop("BINANCE_API_KEY", None)
        os.environ.pop("BINANCE_API_SECRET", None)
        r = preflight.run(cfg, say=lambda s: None)
        self.assertFalse(r.ok)
        self.assertIn("not filled in", "\n".join(r.lines))
        open(os.path.join(d, "keys.txt"), "w").write("BINANCE_API_KEY=PASTE_YOUR_API_KEY_HERE\nBINANCE_API_SECRET=PASTE_YOUR_API_SECRET_HERE\n")
        self.assertFalse(preflight.run(cfg, say=lambda s: None).ok)

    def test_keys_file_parsing(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "keys.txt"), "w", encoding="utf-8-sig").write(
            f'# comment\nBINANCE_API_KEY = "{KEY}"\r\nBINANCE_API_SECRET={SECRET}  \r\n'
        )
        os.environ.pop("BINANCE_API_KEY", None)
        os.environ.pop("BINANCE_API_SECRET", None)
        k, s, prob = K.load_keys(d)
        self.assertEqual((k, s, prob), (KEY, SECRET, None))
        open(os.path.join(d, "keys.txt"), "w").write(f"BINANCE_API_KEY={KEY} oops\nBINANCE_API_SECRET={SECRET}\n")
        self.assertIn("does not look right", K.load_keys(d)[2])

    # ------------------------------------------------------------------ secrets never logged
    def test_secrets_never_in_logs_or_errors(self):
        from desk.logger import Logger

        fx, cfg, cl, lg0, br, risk, ex, tmp = make()
        rl = Logger(cfg)  # the real logger (file logging + redaction filter)
        br.lg = rl
        ex.lg = rl
        fx.reject_keys = True
        with self.assertRaises(B.BinanceError) as c:
            cl.account()
        fx.reject_keys = False
        ex.enter_live([cand()], {}, 1)
        rl.log.info(f"deliberate leak {KEY} {SECRET} signature={'a' * 64}")
        rl.log.error("boom %s", SECRET)
        for h in logging.getLogger().handlers:
            h.flush()
        blob = str(c.exception)
        for root, _, files in os.walk(tmp):
            for f in files:
                blob += open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
        self.assertGreater(len(blob), 500)
        self.assertIn("deliberate leak", blob)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(KEY, blob)
        self.assertNotIn("a" * 64, blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)

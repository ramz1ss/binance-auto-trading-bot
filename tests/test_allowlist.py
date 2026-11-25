"""Symbol allowlist (Binance API-key whitelist): Watcher restriction, -2010 'not whitelisted' handling, blocklist, no phantom state."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from desk import binance as B
from desk import config as C
from desk.models import Order
from desk.watcher import Watcher
from tests.test_live import NullLog, cand, make

EXAMPLE_LIST = (
    "BTCUSDT ETHUSDT BNBUSDT ZECUSDT SOLUSDT XRPUSDT NEARUSDT HYPEUSDT MMTUSDT PEPEUSDT UNIUSDT DOGEUSDT ENAUSDT SUIUSDT TRXUSDT SKYUSDT TAOUSDT LTCUSDT "
    "ADAUSDT AAVEUSDT LINKUSDT BMTUSDT YBUSDT WLDUSDT AVAXUSDT INJUSDT HUMAUSDT MEGAUSDT PUMPUSDT ASTERUSDT CYBERUSDT HBARUSDT FETUSDT ONDOUSDT RAREUSDT SENTUSDT "
    "XLMUSDT BARDUSDT LUNCUSDT JUPUSDT QNTUSDT BCHUSDT"
).split()


class RecLog(NullLog):
    def __init__(self):
        super().__init__()
        self.kills = []

    def kill(self, *a, **k):
        self.kills.append(a)


def tick(sym, qv, last="10", hi="11", lo="9", chg="1"):
    return {"symbol": sym, "quoteVolume": str(qv), "lastPrice": last, "highPrice": hi, "lowPrice": lo, "priceChangePercent": chg}


def watcher(allowed, **over):
    cfg = C.load(mode="paper")
    cfg["watcher"]["allowed_symbols"] = allowed
    cfg["watcher"].update(over)
    return Watcher(cfg, None, RecLog())


TICKERS = [
    tick("BTCUSDT", 9e9),
    tick("ETHUSDT", 5e9),
    tick("BNBUSDT", 1e9),
    tick("SOLUSDT", 8e8),
    tick("SENTUSDT", 6e7),
    tick("FOOUSDT", 7e8),
    tick("NVDABUSDT", 3e8),
    tick("USDCUSDT", 9e9, "1.0", "1.001", "0.999"),
    tick("PEPEUSDT", 2e6),
]


class T(unittest.TestCase):
    # ---------------------------------------------------------------- config + watcher
    def test_shipped_example_config_has_no_allowlist(self):
        cfg = C.load(mode="live")
        self.assertEqual(cfg["watcher"]["allowed_symbols"], [])
        self.assertIsNone(C.allowed_symbols(cfg))
        self.assertEqual(C.allowed_symbols({"watcher": {"allowed_symbols": EXAMPLE_LIST}}), set(EXAMPLE_LIST))

    def test_empty_or_missing_list_means_no_restriction(self):
        self.assertIsNone(C.allowed_symbols({"watcher": {"allowed_symbols": []}}))
        self.assertIsNone(C.allowed_symbols({"watcher": {}}))
        self.assertEqual(C.allowed_symbols({"watcher": {"allowed_symbols": [" sentusdt ", "SOLUSDT", ""]}}), {"SENTUSDT", "SOLUSDT"})

    def test_watcher_only_considers_allowed_symbols_all_of_them(self):
        w = watcher(
            ["SOLUSDT", "SENTUSDT", "PEPEUSDT", "NOPEUSDT"], top_alts=1
        )  # top_alts=1 must NOT cut the list; PEPE is below min_alt_quote_volume
        syms = [s for s, _ in w.universe(TICKERS)]
        self.assertEqual(syms, ["SOLUSDT", "SENTUSDT", "PEPEUSDT"])  # ranked by 24h volume; BTC/ETH/BNB/FOO/BAR absent; unlisted NOPE ignored

    def test_majors_only_if_listed_and_exclusions_still_apply(self):
        w = watcher(["BTCUSDT", "USDCUSDT", "SOLUSDT"])
        self.assertEqual([s for s, _ in w.universe(TICKERS)], ["BTCUSDT", "SOLUSDT"])  # stablecoin (USDC) still excluded
        w = watcher(["SOLUSDT"])
        self.assertEqual([s for s, _ in w.universe(TICKERS)], ["SOLUSDT"])  # majors are NOT added when not listed

    def test_no_allowlist_keeps_the_old_behaviour(self):
        w = watcher([])
        syms = [s for s, _ in w.universe(TICKERS)]
        self.assertEqual(syms[:3], ["BTCUSDT", "ETHUSDT", "BNBUSDT"])
        self.assertIn("FOOUSDT", syms)
        self.assertNotIn("NVDABUSDT", syms)  # NVDABUSDT: tokenized-stock regex
        self.assertNotIn("PEPEUSDT", syms)  # below min_alt_quote_volume

    def test_allowed_set_larger_than_top_alts(self):
        many = [tick(f"C{i:02d}USDT", 1e7 + i) for i in range(30)]
        w = watcher([t["symbol"] for t in many], top_alts=5)
        self.assertEqual(len(w.universe(many)), 30)

    # ---------------------------------------------------------------- error classification
    def test_not_whitelisted_classification(self):
        self.assertTrue(B.BinanceError(400, -2010, "Symbol not whitelisted for API key.").not_whitelisted)
        self.assertFalse(B.BinanceError(400, -2010, "Account has insufficient balance for requested action.").not_whitelisted)
        self.assertFalse(B.BinanceError(400, -2010, "Account has insufficient balance for requested action.").maybe_executed)

    # ---------------------------------------------------------------- -2010 handling in the live path
    def test_whitelist_rejection_is_logged_blocked_and_leaves_no_phantom_position(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        lg2 = RecLog()
        ex.lg = lg2
        br.lg = lg2
        fx.whitelist = {"SOLUSDT"}
        ev = ex.enter_live([cand("SENTUSDT", 0.9), cand("SOLUSDT", 0.8)], {}, 1)  # SENT fails with -2010, next candidate is bought
        self.assertEqual(ev["symbol"], "SOLUSDT")
        self.assertEqual(list(br.positions), ["SOLUSDT"])
        self.assertTrue(br.is_blocked("SENTUSDT"))
        k = [x for x in lg2.kills if x[2] == "SENTUSDT"]
        self.assertEqual(len(k), 1)
        self.assertEqual((k[0][1], k[0][3][0]["check"]), ("executor", "not_whitelisted"))
        saved = json.load(open(os.path.join(tmp, "state", "live_state.json")))
        self.assertEqual(list(saved["positions"]), ["SOLUSDT"])
        self.assertIsNone(saved["pending_entry"])
        self.assertNotIn("SENTUSDT", saved["last_exit"])
        self.assertFalse(br.unresolved)

    def test_blocked_symbol_is_not_retried_next_cycle(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        fx.whitelist = set()
        self.assertIsNone(ex.enter_live([cand("SENTUSDT")], {}, 1))
        n = len([c for c in fx.calls if c[0] == "POST"])
        self.assertEqual(n, 1)  # exactly one attempt
        self.assertEqual(br.positions, {})
        self.assertIsNone(br.s["pending_entry"])
        for cyc in (2, 3, 4):
            self.assertIsNone(ex.enter_live([cand("SENTUSDT")], {}, cyc))
        self.assertEqual(len([c for c in fx.calls if c[0] == "POST"]), n)  # no retry loop
        self.assertEqual(br.cash, 7.0)

    def test_other_2010_errors_are_not_blocklisted(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make(usdt="7.00")
        orig = fx.h_POST_api_v3_order
        fx.h_POST_api_v3_order = lambda p: (
            fx._err(400, -2010, "Account has insufficient balance for requested action.") if p["type"] == "MARKET" else orig(p)
        )
        self.assertIsNone(ex.enter_live([cand("SOLUSDT")], {}, 1))
        self.assertFalse(br.is_blocked("SOLUSDT"))

    # ---------------------------------------------------------------- allowlist guard on every order path
    def test_buy_outside_allowlist_is_refused_without_any_api_call_or_state_change(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        br.allowed = {"SOLUSDT"}
        before = len(fx.calls)
        with self.assertRaises(B.BinanceError) as cm:
            br.buy(Order("SENTUSDT", 100.0, 0.05, 6.86, 0.9), risk)
        self.assertTrue(cm.exception.not_whitelisted)
        self.assertEqual(len([c for c in fx.calls[before:] if c[0] == "POST"]), 0)
        self.assertIsNone(br.s["pending_entry"])
        self.assertEqual(br.positions, {})
        self.assertIsNone(ex.enter_live([cand("SENTUSDT")], {}, 1))
        self.assertEqual(len([c for c in fx.calls[before:] if c[0] == "POST"]), 0)

    def test_stop_and_sell_paths_never_target_a_symbol_outside_the_allowlist(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        ex.enter_live([cand("SOLUSDT")], {}, 1)
        pos = br.positions["SOLUSDT"]
        br.allowed = {"BTCUSDT"}  # allowlist no longer covers the open SOL position
        n = len(fx.calls)
        with self.assertRaises(B.BinanceError):
            br._place_stop(pos, pos["stop"])
        with self.assertRaises(B.BinanceError):
            br._emergency_exit(pos, "test")
        self.assertEqual(br.manage({"SOLUSDT": 100.0}, risk), [])  # would be a stop-loss exit; guard leaves it alone
        self.assertEqual([c for c in fx.calls[n:] if c[0] in ("POST", "DELETE")], [])
        rep = br.reconcile(risk)  # restart must not crash or touch the position
        self.assertTrue(any("not on the allowlist" in r for r in rep))
        self.assertEqual([c for c in fx.calls[n:] if c[0] in ("POST", "DELETE")], [])
        self.assertIn("SOLUSDT", br.positions)

    def test_normal_trading_unchanged_when_symbol_is_allowed(self):
        fx, cfg, cl, lg, br, risk, ex, tmp = make()
        br.allowed = set(EXAMPLE_LIST)
        self.assertIn("SOLUSDT", br.allowed)
        fx.whitelist = set(EXAMPLE_LIST) | {"SOLUSDT"}
        ev = ex.enter_live([cand("SOLUSDT")], {}, 1)
        self.assertEqual(ev["event"], "entry")
        self.assertTrue(br.positions["SOLUSDT"]["stop_order_id"])


if __name__ == "__main__":
    unittest.main()

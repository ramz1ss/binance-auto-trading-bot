import math
import time

from .binance import BinanceError, D, NetworkError, floor_to
from .models import Order
from .util import redact


class Executor:
    """Agent 4: sizes + simulates fills. Balance < $50: one position at a time, ~99% of cash. Otherwise max 10% of equity per trade."""

    def __init__(self, cfg, data, broker, risk, logger):
        self.cfg = cfg
        self.c = cfg["executor"]
        self.d = data
        self.b = broker
        self.risk = risk
        self.lg = logger

    def _filters(self, symbol):
        info = self.d.symbol_info(symbol)
        f = {x["filterType"]: x for x in info["filters"]}
        step = float(f["LOT_SIZE"]["stepSize"])
        minq = float(f["LOT_SIZE"]["minQty"])
        mn = float((f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}).get("minNotional", self.c["min_notional_usd"]))
        return info.get("status"), step, minq, max(mn, self.c["min_notional_usd"])

    def enter(self, ranked, bids, cycle):
        """ranked: score-sorted survivors. Opens at most one new position per cycle. Returns event dict or None."""
        if getattr(self.b, "live", False):
            return self.enter_live(ranked, bids, cycle)
        eq = self.b.equity(bids)
        small = eq < self.c["small_balance_threshold"]
        max_open = 1 if small else self.c["max_open_positions"]
        if len(self.b.positions) >= max_open:
            return None
        for cand in ranked:
            if cand.symbol in self.b.positions:
                continue
            if time.time() - self.b.s["last_exit"].get(cand.symbol, 0) < self.c["reentry_cooldown_min"] * 60:
                continue
            try:
                status, step, minq, minnot = self._filters(cand.symbol)
                if status != "TRADING":
                    continue
                bt = self.d.book_ticker(cand.symbol)
                ask = float(bt["askPrice"])
            except Exception as e:
                self.lg.log.warning("executor quote/filter error %s: %s", cand.symbol, e)
                continue
            budget = self.b.cash / (1 + self.c["fee_rate"])
            notional = budget * self.c["small_balance_fraction"] if small else min(budget, eq * self.c["max_position_fraction"])
            qty = math.floor(notional / ask / step + 1e-9) * step
            qty = float(f"{qty:.12f}")
            if qty < minq or qty * ask < minnot:
                self.lg.skip(cycle, "executor", f"cannot size {cand.symbol}: notional {qty * ask:.2f} < min {minnot} (equity {eq:.2f})", cand.symbol)
                continue
            if small and qty * ask < 0.85 * notional:
                self.lg.skip(
                    cycle, "executor", f"{cand.symbol}: lot step wastes balance ({qty * ask:.2f} of {notional:.2f}); trying next", cand.symbol
                )
                continue
            order = Order(cand.symbol, qty, ask, qty * ask, cand.score)
            if not self.risk.approve(order):
                continue  # Risk sets the -8% stop BEFORE entry
            notional, fee = self.b.buy(order.symbol, order.qty, order.ref_ask, order.stop_price, order.score)
            return {
                "event": "entry",
                "symbol": order.symbol,
                "side": "BUY",
                "qty": order.qty,
                "price": order.ref_ask,
                "notional": notional,
                "fee": fee,
                "score": order.score,
                "stop": order.stop_price,
                "reason": order.risk_note,
                "cash_after": self.b.cash,
                "mode": "paper",
            }
        return None

    def exit(self, symbol, bid, reason, bids):
        p = dict(self.b.positions[symbol])
        notional, fee, pnl, pct = self.b.sell(symbol, bid, reason)
        return {
            "event": "exit",
            "symbol": symbol,
            "side": "SELL",
            "qty": p["qty"],
            "price": bid,
            "notional": notional,
            "fee": fee,
            "pnl": pnl,
            "pnl_pct": pct,
            "reason": reason,
            "stop": p["stop"],
            "cash_after": self.b.cash,
            "equity_after": self.b.equity(bids),
            "mode": "paper",
        }

    # ------------------------------------------------------------------ LIVE
    def plan_live(self, rules, quote, ask):
        """Pure sizing check for a real entry. Returns (ok, reason, est_qty). The stop MUST be placeable after the fill, otherwise we do not buy."""
        L = self.cfg["live"]
        fee = D(self.c["fee_rate"])
        sl = D(self.cfg["risk"]["stop_loss_pct"]) / 100
        buf = D(L.get("stop_limit_buffer_pct", 1.0)) / 100
        quote = D(quote)
        ask = D(ask)
        if quote < rules.min_notional * D("1.0"):
            return False, f"order {quote} below Binance minimum {rules.min_notional}", None
        gross = rules.qty_down(quote / ask)  # what the exchange will fill (rounded down to the lot step)
        est_qty = rules.qty_down(gross * (1 - fee))  # what we can sell: Binance takes its fee in the coin, which often costs one lot step
        if gross < rules.min_qty or est_qty < rules.min_qty:
            return False, f"quantity {est_qty} below lot minimum {rules.min_qty}", None
        # worst case the stop is placed at the lowest price we ever use (initial stop); its limit leg is below the stop
        lim = rules.price_down(ask * (1 - sl) * (1 - buf) * D("0.995"))
        if lim <= 0 or est_qty * lim < rules.min_notional:
            return (
                False,
                f"protective stop would be {est_qty * lim:.2f}, below Binance minimum {rules.min_notional} (balance too small to protect)",
                None,
            )
        return True, "", (est_qty, gross)

    def enter_live(self, ranked, bids, cycle):
        L = self.cfg["live"]
        if self.b.unresolved:
            self.b.reconcile(self.risk)
            if self.b.unresolved:
                self.lg.skip(cycle, "executor", "an earlier order is still unconfirmed; not buying")
                return None
        self.b.refresh()
        eq = self.b.equity(bids)
        small = eq < self.c["small_balance_threshold"]
        if len(self.b.positions) >= (1 if small else self.c["max_open_positions"]):
            return None
        free = D(self.b.cash)
        frac = D(L.get("small_balance_fraction", 0.98))  # Decimal: 7 * 0.98 must be exactly 6.86
        quote_total = free * frac if small else min(D(eq) * D(self.c["max_position_fraction"]), free * frac)
        quote_total = floor_to(quote_total, "0.01")
        for cand in ranked:
            if cand.symbol in self.b.positions:
                continue
            if not self.b.symbol_allowed(cand.symbol):
                continue  # never buy outside the allowlist
            if self.b.is_blocked(cand.symbol):
                continue  # session blocklist (e.g. not whitelisted by Binance): no retry loop
            if time.time() - self.b.s["last_exit"].get(cand.symbol, 0) < self.c["reentry_cooldown_min"] * 60:
                continue
            try:
                R = self.b.rules(cand.symbol)
                prob = R.tradable_problem()
                if prob:
                    self.lg.skip(cycle, "executor", f"{cand.symbol}: {prob}", cand.symbol)
                    continue
                ask = float(self.d.book_ticker(cand.symbol)["askPrice"])
            except Exception as e:
                self.lg.log.warning("executor quote/filter error %s: %s", cand.symbol, redact(e))
                continue
            ok, why, qtys = self.plan_live(R, quote_total, ask)
            if not ok:
                self.lg.skip(cycle, "executor", f"cannot size {cand.symbol}: {why} (equity {eq:.2f})", cand.symbol)
                continue
            est_qty, gross = qtys
            if small and float(est_qty) * ask < 0.90 * float(quote_total):  # lot step (+ fee paid in coin) would strand >10% as dust
                self.lg.skip(
                    cycle,
                    "executor",
                    f"{cand.symbol}: lot step wastes balance ({float(est_qty) * ask:.2f} sellable of {quote_total}); trying next",
                    cand.symbol,
                )
                continue
            order = Order(cand.symbol, float(est_qty), ask, quote_total, cand.score)
            if not self.risk.approve(order):
                continue  # Risk sets the -8% stop plan BEFORE entry
            try:
                ev = self.b.buy(order, self.risk)
            except (BinanceError, NetworkError) as e:
                self.lg.log.error("BUY %s failed: %s", cand.symbol, redact(e))
                if getattr(e, "not_whitelisted", False):
                    self.b.block(cand.symbol, redact(str(e)))
                    reason = {
                        "check": "not_whitelisted",
                        "value": None,
                        "threshold": None,
                        "detail": f"Binance refused {cand.symbol}: the API key's symbol whitelist does not include it. Blocked for this session; add it to the key's whitelist and to allowed_symbols in config.toml.",
                    }
                    self.lg.kill(cycle, "executor", cand.symbol, [reason], cand.score)
                    continue  # nothing was bought; try the next candidate
                if getattr(e, "maybe_executed", False):
                    return None  # outcome unclear: stop for this cycle, reconcile next
                continue  # clear rejection: try the next candidate
            if ev:
                return ev
        return None

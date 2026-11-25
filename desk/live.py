"""LIVE broker for Binance SPOT. Same surface as PaperBroker (cash / positions / equity / s / save) plus:
  reconcile()  - at startup (and after any unclear order) sync local state with the REAL account so a restart never double-buys
  buy()        - market BUY via quoteOrderQty, then the -8% stop goes on the exchange right after the fill
  manage()     - every tick: detect stop fills, raise the trailing stop (cancel + replace), safety-exit if a stop-limit gets gapped through
Every real order carries a client id starting with "desk", so the desk can recognise its own orders and ignore everything else on the account."""

import itertools
import json
import os
import time
from decimal import Decimal

from .binance import BinanceError, D, NetworkError, SymbolRules, fmt, market_buy_params, market_sell_params, oco_params, stop_limit_params
from .config import allowed_symbols
from .util import atomic_write_json, iso, redact


class StopPlacementError(Exception):
    pass


class LiveBroker:
    live = True

    def __init__(self, cfg, client, data, logger, sleep=time.sleep):
        self.cfg, self.L, self.c, self.d, self.lg, self.sleep = cfg, cfg["live"], client, data, logger, sleep
        self.fee_rate = cfg["executor"]["fee_rate"]
        self.sl = cfg["risk"]["stop_loss_pct"] / 100
        self.quote = self.L.get("quote_asset", "USDT")
        self.path = os.path.join(cfg["_data"], "state", "live_state.json")
        self.s = {
            "positions": {},
            "pending_entry": None,
            "realized_pnl": 0.0,
            "fees_paid": 0.0,
            "closed": 0,
            "wins": 0,
            "last_exit": {},
            "started": iso(),
            "start_equity": None,
        }
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.s.update(json.load(f))
        self.balances = {}
        self.unresolved = False
        self.events = []
        self._rules = {}
        self._n = itertools.count(1)
        self.foreign = []
        self.allowed = allowed_symbols(cfg)  # None = no restriction; otherwise the ONLY symbols any order may target
        self.blocked = {}  # session blocklist: symbol -> reason (cleared on restart)

    # ------------------------------------------------------------------ allowlist / blocklist
    def symbol_allowed(self, sym):
        return self.allowed is None or sym in self.allowed

    def is_blocked(self, sym):
        return sym in self.blocked

    def block(self, sym, why):
        """Stop trying to BUY this symbol for the rest of the session (e.g. Binance says it is not whitelisted for the API key)."""
        if sym not in self.blocked:
            self.lg.log.warning("blocklisted %s for this session: %s", sym, why)
        self.blocked[sym] = why

    def _guard(self, sym):
        """Every order (buy, stop, trailing replace, market sell) goes through here: a symbol outside the allowlist is refused locally,
        before anything is sent to Binance. Raised as a clear (non-retryable) Binance-style rejection."""
        if not self.symbol_allowed(sym):
            raise BinanceError(400, -2010, f"Symbol {sym} not whitelisted (not in the desk allowlist); order refused locally")

    # ------------------------------------------------------------------ small helpers
    def save(self):
        atomic_write_json(self.path, self.s)

    @property
    def positions(self):
        return self.s["positions"]

    def _bal(self, asset):
        return self.balances.get(asset, (Decimal(0), Decimal(0)))

    @property
    def cash(self):
        return float(self._bal(self.quote)[0])  # FREE quote asset (USDT)

    def equity(self, bids):
        q = sum(self._bal(self.quote))
        return float(q) + sum(p["qty"] * (bids.get(sym) or p["entry"]) for sym, p in self.positions.items())

    def _cid(self, kind):
        return f"desk{kind}{int(time.time() * 1000)}{next(self._n) % 1000}"  # <=36 chars, only [A-Za-z0-9]

    def drain_events(self):
        ev, self.events = self.events, []
        return ev

    def rules(self, symbol):
        r = self._rules.get(symbol)
        if r is None or time.time() - r[0] > 3600:
            r = (time.time(), SymbolRules(self.c.exchange_info(symbol)))
            self._rules[symbol] = r
        return r[1]

    def refresh(self):
        acc = self.c.account()
        self.balances = {b["asset"]: (D(b["free"]), D(b["locked"])) for b in acc.get("balances", [])}
        self.can_trade = acc.get("canTrade", False)
        return acc

    def _find_order(self, symbol, client_id, tries=3):
        """Look an order up by our client id. Returns the order dict, or None when Binance says it does not exist."""
        for i in range(tries):
            try:
                return self.c.get_order(symbol, client_id=client_id)
            except BinanceError as e:
                if e.unknown_order:
                    return None
                if i + 1 == tries:
                    raise
            except NetworkError:
                if i + 1 == tries:
                    raise
            self.sleep(1.5)

    def _send_order(self, params, kind="order"):
        """POST an order. If the outcome is unclear (timeout/5xx) look it up by client id instead of blindly re-sending."""
        sym, cid = params["symbol"], params.get("newClientOrderId")
        self._guard(sym)
        try:
            return self.c.new_order(params)
        except (BinanceError, NetworkError) as e:
            if not getattr(e, "maybe_executed", False):
                raise
            self.lg.log.warning("%s %s outcome unclear (%s); looking it up by client id", kind, sym, redact(e))
            o = self._find_order(sym, cid)
            if o is None:
                raise
            return o

    # ------------------------------------------------------------------ fills
    def _fills(self, order):
        f = order.get("fills")
        if f is None:
            try:
                f = self.c.my_trades(order["symbol"], order_id=order["orderId"])
            except Exception:
                f = []
        return f

    def _fill_summary(self, order, rules):
        ex, quote = D(order["executedQty"]), D(order["cummulativeQuoteQty"])
        base_fee, fee_q = Decimal(0), Decimal(0)
        fills = self._fills(order)
        for f in fills:
            com, asset = D(f.get("commission", "0")), f.get("commissionAsset")
            price = D(f.get("price", "0"))
            if asset == rules.base:
                base_fee += com
                fee_q += com * price
            elif asset == self.quote:
                fee_q += com
            else:
                fee_q += D(f.get("qty", "0")) * price * D("0.00075")  # e.g. paid in BNB: rough estimate only
        if not fills and rules.base:
            base_fee = ex * D(self.fee_rate)
            fee_q = quote * D(self.fee_rate)
        return ex, quote, base_fee, fee_q

    # ------------------------------------------------------------------ stops (on the exchange)
    def _place_stop(self, pos, stop):
        """Put (or re-put) the protective SELL on Binance and record ids in pos. stop = trigger price (float)."""
        sym = pos["symbol"]
        self._guard(sym)
        R = self.rules(sym)
        qty = D(pos["qty_str"])
        stop_p = R.price_down(stop)
        lim_p = R.price_down(D(stop) * (1 - D(self.L.get("stop_limit_buffer_pct", 1.0)) / 100))
        if lim_p >= stop_p:
            lim_p = stop_p - R.tick
        if lim_p <= 0 or lim_p < R.min_price:
            raise StopPlacementError(f"stop price {stop} too small for {sym}")
        if qty < R.min_qty or qty * lim_p < R.min_notional:
            raise StopPlacementError(f"{sym}: stop order would be {qty * lim_p:.4f} {self.quote}, below Binance minimum {R.min_notional}")
        mode = self.L.get("stop_mode", "stop_limit")
        scid = self._cid("S")
        try:
            if mode == "oco":
                tp = R.price_down(D(pos["entry"]) * (1 + D(self.L.get("oco_take_profit_pct", 300.0)) / 100))
                p = oco_params(sym, qty, stop_p, lim_p, tp, scid, self._cid("T"), self._cid("L"))
                r = self.c.new_oco(p)
                legs = {o["clientOrderId"]: o["orderId"] for o in r.get("orders", [])}
                pos["order_list_id"] = r["orderListId"]
                pos["stop_order_id"] = legs[p["belowClientOrderId"]]
                pos["tp_order_id"] = legs[p["aboveClientOrderId"]]
                pos["stop_client_id"] = scid
            else:
                r = self._send_order(stop_limit_params(sym, qty, stop_p, lim_p, scid), "stop")
                pos["order_list_id"] = None
                pos["tp_order_id"] = None
                pos["stop_order_id"] = r["orderId"]
                pos["stop_client_id"] = scid
        except (BinanceError, NetworkError) as e:
            if isinstance(e, BinanceError) and e.would_trigger:
                raise
            if getattr(e, "maybe_executed", False) or isinstance(e, NetworkError):
                o = self._find_order(sym, scid)  # did it land after all?
                if o is not None:
                    pos["stop_order_id"] = o["orderId"]
                    pos["stop_client_id"] = scid
                    pos["order_list_id"] = o.get("orderListId") if o.get("orderListId", -1) != -1 else None
                else:
                    raise
            else:
                raise StopPlacementError(redact(e)) from None
        pos["stop"] = float(stop_p)
        pos["stop_limit"] = float(lim_p)
        self.save()

    def _relink_open_stop(self, pos):
        """If a desk stop order for this coin is already open on Binance (our record lost its id), adopt it instead of placing a second one."""
        for o in self.c.open_orders(pos["symbol"]):
            if str(o.get("clientOrderId", "")).startswith("deskS") and o.get("side") == "SELL":
                lid = o.get("orderListId", -1)
                pos.update(
                    stop_order_id=o["orderId"],
                    stop_client_id=o["clientOrderId"],
                    order_list_id=lid if lid != -1 else None,
                    stop=float(o["stopPrice"]),
                    stop_limit=float(o["price"]),
                )
                self.save()
                return True
        return False

    def _place_stop_retry(self, pos, stop, tries=3):
        last = None
        for i in range(tries):
            if i > 0:
                try:
                    if self._relink_open_stop(pos):
                        return
                except (BinanceError, NetworkError):
                    pass
            try:
                return self._place_stop(pos, stop)
            except StopPlacementError:
                raise
            except BinanceError as e:
                if e.would_trigger:
                    raise
                last = e
            except NetworkError as e:
                last = e
            self.sleep(1 + i)
        raise last

    def _cancel_stop(self, pos):
        """Cancel the exchange stop (single order or whole OCO list). Returns True if cancelled, False if it no longer existed."""
        self._guard(pos["symbol"])
        try:
            if pos.get("order_list_id") is not None:
                self.c.cancel_order_list(pos["symbol"], pos["order_list_id"])
            elif pos.get("stop_order_id") is not None:
                self.c.cancel_order(pos["symbol"], order_id=pos["stop_order_id"])
            else:
                return False
            return True
        except BinanceError as e:
            if e.unknown_order or "unknown" in (e.msg or "").lower():
                return False
            raise

    def _stop_state(self, pos):
        """-> (state, order) with state in OPEN | PARTIAL | FILLED | GONE."""
        sym = pos["symbol"]
        if pos.get("stop_order_id") is None:
            return "GONE", None
        try:
            o = self.c.get_order(sym, order_id=pos["stop_order_id"])
        except BinanceError as e:
            if e.unknown_order:
                return "GONE", None
            raise
        if pos.get("tp_order_id") is not None:
            try:
                tp = self.c.get_order(sym, order_id=pos["tp_order_id"])
                if tp["status"] == "FILLED":
                    return "FILLED", tp
            except BinanceError:
                pass
        st = o["status"]
        if st == "FILLED":
            return "FILLED", o
        if st == "PARTIALLY_FILLED":
            return "PARTIAL", o
        if st in ("NEW", "PENDING_NEW"):
            return "OPEN", o
        return "GONE", o

    # ------------------------------------------------------------------ exits
    def _finalize_exit(self, pos, qty, quote_gross, reason):
        sym = pos["symbol"]
        qty, quote_gross = float(qty), float(quote_gross)
        fee = quote_gross * self.fee_rate
        net = quote_gross - fee
        pnl = net - pos["cost"]
        pct = pnl / pos["cost"] * 100 if pos["cost"] else 0.0
        self.positions.pop(sym, None)
        self.s["realized_pnl"] += pnl
        self.s["fees_paid"] += fee + pos.get("entry_fee", 0.0)
        self.s["closed"] += 1
        self.s["wins"] += pnl > 0
        self.s["last_exit"][sym] = time.time()
        self.save()
        try:
            self.refresh()
        except Exception:
            pass
        ev = {
            "event": "exit",
            "symbol": sym,
            "side": "SELL",
            "qty": qty,
            "price": quote_gross / qty if qty else 0.0,
            "notional": quote_gross,
            "fee": fee,
            "pnl": pnl,
            "pnl_pct": pct,
            "reason": reason,
            "stop": pos.get("stop"),
            "cash_after": self.cash,
            "equity_after": self.equity({}),
            "mode": "live",
        }
        self.events.append(ev)
        return ev

    def _emergency_exit(self, pos, reason):
        """Cancel the stop and dump the position at market. Used when a stop cannot be placed / was gapped through."""
        sym = pos["symbol"]
        self._guard(sym)
        R = self.rules(sym)
        try:
            self._cancel_stop(pos)
        except Exception as e:
            self.lg.log.warning("emergency exit %s: cancel failed: %s", sym, redact(e))
        st, o = self._stop_state(pos) if pos.get("stop_order_id") is not None else ("GONE", None)
        if st == "FILLED":
            return self._finalize_exit(pos, o["executedQty"], o["cummulativeQuoteQty"], pos.get("trailing") and "trailing_stop" or "stop_loss")
        self.refresh()
        free = self._bal(R.base)[0]
        qty = R.qty_down(min(D(pos["qty_str"]), free))
        pos["stop_order_id"] = None
        pos["order_list_id"] = None
        if qty < R.min_qty:
            self.lg.log.error("emergency exit %s: nothing sellable left (free=%s) - closing record", sym, free)
            return self._finalize_exit(pos, 0, 0, "closed_outside_desk")
        r = self._send_order(market_sell_params(sym, qty, self._cid("X")), "market sell")
        ex, quote = D(r["executedQty"]), D(r["cummulativeQuoteQty"])
        if ex <= 0:
            raise RuntimeError(f"market sell of {sym} did not fill: {r.get('status')}")
        return self._finalize_exit(pos, ex, quote, reason)

    def _replace_stop(self, pos, new_stop):
        """Trailing: cancel the old stop and place the higher one. If anything fails we sell at market rather than stay unprotected."""
        sym = pos["symbol"]
        old = pos["stop"]
        try:
            cancelled = self._cancel_stop(pos)
        except NetworkError:
            return  # nothing changed on our side; retry next tick
        except BinanceError as e:
            if e.maybe_executed:
                return
            raise
        if not cancelled:  # it vanished: probably just filled
            st, o = self._stop_state(pos)
            if st == "FILLED":
                return self._finalize_exit(pos, o["executedQty"], o["cummulativeQuoteQty"], "trailing_stop" if pos["trailing"] else "stop_loss")
        try:
            self._place_stop_retry(pos, new_stop)
        except BinanceError as e:
            if e.would_trigger:
                return self._emergency_exit(pos, "trailing_stop_market_exit")
            return self._emergency_exit(pos, "stop_replace_failed_market_exit")
        except (StopPlacementError, NetworkError) as e:
            self.lg.log.error("stop replace failed for %s: %s - selling at market", sym, redact(e))
            return self._emergency_exit(pos, "stop_replace_failed_market_exit")
        self.events.append(
            {
                "event": "stop_move",
                "symbol": sym,
                "side": "",
                "qty": pos["qty"],
                "price": pos["peak"],
                "stop": pos["stop"],
                "reason": f"trailing: peak {pos['peak']:.8g} -> stop {old:.8g} to {pos['stop']:.8g} (stop_limit {pos['stop_limit']:.8g})",
                "mode": "live",
            }
        )

    # ------------------------------------------------------------------ entry
    def buy(self, order, risk):
        """Market BUY spending order.notional USDT, then place the stop. Returns the entry event, or None if nothing was bought."""
        sym = order.symbol
        self._guard(sym)  # outside the allowlist: refuse before any state change or API call
        if self.is_blocked(sym):
            raise BinanceError(400, -2010, f"Symbol {sym} is on this session's blocklist ({self.blocked[sym]}); not retrying")
        self.rules(sym)  # validates/caches the symbol's exchange rules (raises if unavailable)
        cid = self._cid("B")
        self.s["pending_entry"] = {"symbol": sym, "client_id": cid, "quote": str(order.notional), "score": order.score, "ts": iso()}
        self.save()
        try:
            resp = self._send_order(market_buy_params(sym, order.notional, cid), "buy")
        except BinanceError as e:
            if not e.maybe_executed:
                self.s["pending_entry"] = None
                self.save()  # clear rejection: nothing was bought, no position is recorded
            else:
                self.unresolved = True
            if e.not_whitelisted:
                self.block(sym, f"HTTP {e.status} code={e.code}: {e.msg}")
            raise
        except NetworkError:
            self.unresolved = True
            raise
        if D(resp.get("executedQty", "0")) <= 0:
            self.s["pending_entry"] = None
            self.save()
            self.lg.log.warning("buy %s: nothing filled (%s)", sym, resp.get("status"))
            return None
        pos = self._adopt_fill(resp, order.score, risk)
        return {
            "event": "entry",
            "symbol": sym,
            "side": "BUY",
            "qty": pos["qty"],
            "price": pos["entry"],
            "notional": pos["cost"],
            "fee": pos["entry_fee"],
            "score": order.score,
            "stop": pos["stop"],
            "reason": f"filled; initial stop -{self.sl * 100:.0f}% placed on Binance (stop {pos['stop']:.8g}, limit {pos.get('stop_limit', 0):.8g})"
            if pos.get("stop_order_id")
            else "filled; STOP FAILED -> emergency exit",
            "cash_after": self.cash,
            "mode": "live",
        }

    def _adopt_fill(self, resp, score, risk):
        sym = resp["symbol"]
        R = self.rules(sym)
        ex, quote, base_fee, fee_q = self._fill_summary(resp, R)
        self.refresh()
        free_base = self._bal(R.base)[0]
        sell_qty = ex - base_fee
        if free_base > 0:
            sell_qty = min(sell_qty, free_base)
        sell_qty = R.qty_down(sell_qty)
        entry = float(quote / ex)
        now = int(time.time() * 1000)
        pos = {
            "symbol": sym,
            "base": R.base,
            "qty": float(sell_qty),
            "qty_str": fmt(sell_qty),
            "entry": entry,
            "entry_fee": float(fee_q),
            "cost": float(quote),
            "entry_ts": iso(),
            "entry_ms": now,
            "stop": risk.stop_for(entry),
            "initial_stop": risk.stop_for(entry),
            "peak": entry,
            "trailing": False,
            "score": score,
            "last_candle_open": (now // 60000) * 60000,
            "stop_order_id": None,
            "stop_client_id": None,
            "order_list_id": None,
            "tp_order_id": None,
            "stop_limit": None,
            "buy_order_id": resp.get("orderId"),
        }
        self.positions[sym] = pos
        self.s["pending_entry"] = None
        self.save()  # recorded BEFORE the stop goes on
        try:
            self._place_stop_retry(pos, pos["initial_stop"])
            pos["initial_stop"] = pos["stop"]
        except (StopPlacementError, BinanceError, NetworkError) as e:
            self.lg.log.error("could not place the stop for %s (%s) - selling immediately", sym, redact(e))
            self._emergency_exit(pos, "stop_failed_emergency_exit")
        self.save()
        return pos

    # ------------------------------------------------------------------ per-tick management
    def manage(self, bids, risk):
        for sym in list(self.positions):
            pos = self.positions.get(sym)
            if not self.symbol_allowed(sym):  # never send a stop/sell for a symbol outside the allowlist
                if sym not in self.blocked:
                    self.block(sym, "open position outside the allowlist: left alone")
                continue
            try:
                self._manage_one(pos, bids.get(sym), risk)
            except (BinanceError, NetworkError) as e:
                self.lg.log.warning("manage %s: %s", sym, redact(e))
            except Exception as e:
                self.lg.log.error("manage %s unexpected: %s: %s", sym, e.__class__.__name__, redact(e))
        self.save()
        return self.drain_events()

    def _manage_one(self, pos, bid, risk):
        sym = pos["symbol"]
        st, o = self._stop_state(pos)
        if st == "FILLED":
            tp = pos.get("tp_order_id") is not None and o["orderId"] == pos["tp_order_id"]
            self._finalize_exit(
                pos, o["executedQty"], o["cummulativeQuoteQty"], "take_profit" if tp else ("trailing_stop" if pos["trailing"] else "stop_loss")
            )
            return
        if st == "GONE":
            self.refresh()
            have = sum(self._bal(pos["base"]))
            if have < D(pos["qty_str"]) / 2:
                self.lg.log.warning("%s: position and stop are gone and the coins are no longer in the account -> recording as closed", sym)
                self._finalize_exit(pos, pos["qty"], pos["qty"] * (bid or pos["entry"]), "closed_outside_desk")
                return
            if self._relink_open_stop(pos):
                return
            self.lg.log.warning("%s: exchange stop is missing - placing it again", sym)
            try:
                self._place_stop_retry(pos, pos["stop"])
            except BinanceError as e:
                if e.would_trigger:
                    self._emergency_exit(pos, "stop_missing_market_exit")
                    return
                raise
            except StopPlacementError:
                self._emergency_exit(pos, "stop_missing_market_exit")
                return
            return
        new_stop = risk.live_update(pos, bid, self.L.get("trail_min_step_pct", 0.25) / 100)  # tracks the peak
        if bid and pos.get("stop_limit") and bid < pos["stop_limit"]:
            self.lg.log.warning("%s: price %.8g is below the stop-limit %.8g (gapped through) -> market exit", sym, bid, pos["stop_limit"])
            self._emergency_exit(pos, "stop_gap_market_exit")
            return
        if new_stop and st == "OPEN":
            self._replace_stop(pos, new_stop)

    # ------------------------------------------------------------------ manual close (CLOSE-TRADE.bat / the visualizer button)
    def close_all(self, symbols=None):
        """Sell the desk's open positions at market now (all of them, or only `symbols`). Returns one result dict per position:
        {symbol, status: closed|already_closed|failed|skipped, detail, pnl, pnl_pct}. Never raises."""
        out = []
        for sym in list(self.positions):
            if symbols and sym not in symbols:
                continue
            try:
                out.append(self.manual_close(sym))
            except Exception as e:  # one failing coin must not stop the others, and must never crash the desk
                self.lg.log.error("manual close %s failed: %s: %s", sym, e.__class__.__name__, redact(e))
                out.append(
                    {
                        "symbol": sym,
                        "status": "failed",
                        "detail": f"{e.__class__.__name__}: {redact(e)}. Nothing was changed that you need to fix by hand - check Binance > Orders.",
                    }
                )
        return out

    def manual_close(self, sym):
        """Close ONE position by hand: cancel the desk's stop, market-sell the free quantity, record the trade (reason 'manual'), start the re-entry cooldown.
        Order of safety: (1) stop already filled? record that, sell nothing; (2) can the coins be sold at all (lot size / min notional)? if not, leave the stop alone;
        (3) cancel the stop; (4) sell; if the sell fails the stop is put back so the position is never left unprotected."""
        pos = self.positions.get(sym)
        if pos is None:
            return {"symbol": sym, "status": "skipped", "detail": "no open position"}
        if not self.symbol_allowed(sym):
            return {
                "symbol": sym,
                "status": "skipped",
                "detail": "not on the allowlist - the desk never touches it; close it by hand in the Binance app",
            }
        R = self.rules(sym)
        sold_q, sold_quote = Decimal(0), Decimal(0)

        def done(ev, status, detail):
            return {
                "symbol": sym,
                "status": status,
                "detail": detail,
                "pnl": ev.get("pnl"),
                "pnl_pct": ev.get("pnl_pct"),
                "price": ev.get("price"),
                "qty": ev.get("qty"),
            }

        def filled_reason(o):
            return (
                "take_profit"
                if (pos.get("tp_order_id") is not None and o["orderId"] == pos["tp_order_id"])
                else ("trailing_stop" if pos["trailing"] else "stop_loss")
            )

        st, o = self._stop_state(pos)  # (1)
        if st == "FILLED":
            ev = self._finalize_exit(pos, o["executedQty"], o["cummulativeQuoteQty"], filled_reason(o))
            return done(ev, "already_closed", "its stop-loss had already been hit, so nothing was sold; recorded as " + ev["reason"])
        if st == "PARTIAL":
            sold_q, sold_quote = D(o["executedQty"]), D(o["cummulativeQuoteQty"])
        remaining = max(Decimal(0), D(pos["qty_str"]) - sold_q)
        self.refresh()
        total = sum(self._bal(R.base))  # coins locked in the stop order count: they are freed by the cancel
        if total < remaining / 2:
            ev = self._finalize_exit(pos, pos["qty"], pos["qty"] * pos["entry"], "closed_outside_desk")
            return done(ev, "already_closed", "the coins are no longer in the account (sold or moved elsewhere); position closed in the records")
        sellable = R.qty_down(min(remaining, total))
        try:
            bid = float(self.d.book_ticker(sym)["bidPrice"])
        except Exception:
            bid = None
        if sellable < R.min_qty or (bid is not None and float(sellable) * bid < float(R.min_notional)):  # (2) dust: Binance would refuse the sale
            return {
                "symbol": sym,
                "status": "failed",
                "detail": f"too small to sell ({sellable} {R.base}"
                + (f" = {float(sellable) * bid:.2f} {self.quote}" if bid else "")
                + f", Binance minimum {R.min_notional} {self.quote}); its stop-loss was left in place",
            }
        try:
            cancelled = self._cancel_stop(pos)  # (3)
        except (BinanceError, NetworkError) as e:
            return {
                "symbol": sym,
                "status": "failed",
                "detail": f"could not cancel the stop-loss ({redact(e)}); nothing was sold and the stop-loss is still active",
            }
        if not cancelled or st == "PARTIAL":  # it vanished or was partially hit: look again, it may have filled in the meantime
            st2, o2 = self._stop_state(pos) if pos.get("stop_order_id") is not None else ("GONE", None)
            if st2 == "FILLED":
                ev = self._finalize_exit(pos, o2["executedQty"], o2["cummulativeQuoteQty"], filled_reason(o2))
                return done(ev, "already_closed", "its stop-loss was hit while closing, so nothing more was sold; recorded as " + ev["reason"])
            if o2 is not None and o2.get("executedQty") is not None:
                sold_q, sold_quote = D(o2["executedQty"]), D(o2["cummulativeQuoteQty"])
        pos["stop_order_id"] = None
        pos["order_list_id"] = None
        self.save()
        self.refresh()
        free = self._bal(R.base)[0]
        qty = R.qty_down(min(max(Decimal(0), D(pos["qty_str"]) - sold_q), free))
        if qty < R.min_qty:
            if sold_q > 0:
                ev = self._finalize_exit(pos, sold_q, sold_quote, "manual")
                return done(ev, "closed", "closed (the stop had already sold most of it)")
            ev = self._finalize_exit(pos, pos["qty"], pos["qty"] * pos["entry"], "closed_outside_desk")
            return done(ev, "already_closed", "nothing sellable left in the account; position closed in the records")
        try:  # (4)
            r = self._send_order(market_sell_params(sym, qty, self._cid("X")), "manual sell")
            ex, quote = D(r["executedQty"]), D(r["cummulativeQuoteQty"])
            if ex <= 0:
                raise RuntimeError(f"market sell did not fill: {r.get('status')}")
        except Exception as e:
            self.lg.log.error("manual close %s: the sell failed (%s) - putting the stop-loss back", sym, redact(e))
            try:
                self._place_stop_retry(pos, pos["stop"])
                back = "its stop-loss was put back"
            except Exception as e2:
                back = f"WARNING: the stop-loss could not be put back yet ({redact(e2)}); the desk will retry it within 30 seconds"
            return {"symbol": sym, "status": "failed", "detail": f"the sell failed ({redact(e)}); {back}. Try again, or sell in the Binance app."}
        if ex < qty:  # partial market fill (rare on spot): sell the rest once
            rest = R.qty_down(qty - ex)
            if rest >= R.min_qty and (bid is None or float(rest) * bid >= float(R.min_notional)):
                try:
                    r2 = self._send_order(market_sell_params(sym, rest, self._cid("X")), "manual sell (rest)")
                    ex += D(r2["executedQty"])
                    quote += D(r2["cummulativeQuoteQty"])
                except Exception as e:
                    self.lg.log.warning("manual close %s: could not sell the remaining %s: %s", sym, rest, redact(e))
        ev = self._finalize_exit(pos, sold_q + ex, sold_quote + quote, "manual")
        dust = self._bal(R.base)[0]
        note = "sold at market; the trade is recorded with reason 'manual'"
        if dust > 0:
            note += f" ({dust} {R.base} dust left in the wallet - Binance can convert it to BNB)"
        return done(ev, "closed", note)

    # ------------------------------------------------------------------ startup reconciliation
    def reconcile(self, risk):
        """Make local state match the REAL account. Safe to call repeatedly. Returns list of plain-English report lines."""
        rep = []
        self.refresh()
        pe = self.s.get("pending_entry")
        if pe:
            try:
                o = self._find_order(pe["symbol"], pe["client_id"])
            except (BinanceError, NetworkError) as e:
                self.unresolved = True
                rep.append(f"Could not check an earlier unfinished buy of {pe['symbol']} ({redact(e)}); not buying until it is resolved.")
                return rep
            if o and D(o.get("executedQty", "0")) > 0 and pe["symbol"] not in self.positions:
                self._adopt_fill(o, pe.get("score"), risk)
                rep.append(f"Found an earlier buy of {pe['symbol']} that was interrupted; it is now protected by a stop.")
            else:
                rep.append(f"An earlier buy of {pe['symbol']} never went through; cleared.")
            self.s["pending_entry"] = None
            self.save()
        self.unresolved = False
        oo = self.c.open_orders()
        mine = [o for o in oo if str(o.get("clientOrderId", "")).startswith("deskS") and o.get("side") == "SELL"]
        rep.append(
            f"{len(oo)} open order(s) on the account; {len(mine)} are this desk's stop orders"
            + (f", {len(oo) - len(mine)} belong to you and are ignored." if len(oo) > len(mine) else ".")
        )
        by_sym = {}
        for o in mine:
            by_sym.setdefault(o["symbol"], o)
        for sym in list(self.positions):  # 1) positions we remember
            pos = self.positions[sym]
            o = by_sym.get(sym)
            if not self.symbol_allowed(sym):
                rep.append(f"{sym}: not on the allowlist; the desk will NOT place, move or cancel any order for it (check it by hand on Binance).")
                continue
            if o and o["orderId"] == pos.get("stop_order_id"):
                rep.append(f"{sym}: position still protected by its stop (stop {o.get('stopPrice')}).")
                continue
            if o:  # same symbol, other id -> re-link to the live stop
                pos["stop_order_id"] = o["orderId"]
                pos["stop_client_id"] = o["clientOrderId"]
                pos["order_list_id"] = o.get("orderListId") if o.get("orderListId", -1) != -1 else None
                pos["stop"] = float(o["stopPrice"])
                pos["stop_limit"] = float(o["price"])
                rep.append(f"{sym}: re-linked to its open stop order.")
                continue
            st, fo = self._stop_state(pos)
            if st == "FILLED":
                self._finalize_exit(pos, fo["executedQty"], fo["cummulativeQuoteQty"], "stop_filled_while_desk_was_off")
                rep.append(f"{sym}: its stop was hit while the desk was off; recorded the exit.")
            else:
                self.refresh()
                have = sum(self._bal(pos["base"]))
                if have < D(pos["qty_str"]) / 2:
                    self._finalize_exit(pos, pos["qty"], pos["qty"] * pos["entry"], "closed_outside_desk")
                    rep.append(f"{sym}: coins are no longer in the account; position closed in the records.")
                else:
                    try:
                        self._place_stop_retry(pos, pos["stop"])
                        rep.append(f"{sym}: stop order was missing - placed it again.")
                    except Exception as e:
                        rep.append(f"{sym}: stop order was missing and could not be re-placed ({redact(e)}); selling at market.")
                        self._emergency_exit(pos, "stop_missing_market_exit")
        for sym, o in by_sym.items():  # 2) desk stop orders we do not remember (state file lost)
            if sym in self.positions:
                continue
            R = self.rules(sym)
            qty = D(o["origQty"])
            entry = None
            try:
                bs = [t for t in self.c.my_trades(sym, limit=20) if t.get("isBuyer")]
                if bs:
                    entry = float(bs[-1]["price"])
            except Exception:
                pass
            stop = float(o["stopPrice"])
            entry = entry or stop / (1 - self.sl)
            now = int(time.time() * 1000)
            self.positions[sym] = {
                "symbol": sym,
                "base": R.base,
                "qty": float(qty),
                "qty_str": fmt(qty),
                "entry": entry,
                "entry_fee": 0.0,
                "cost": float(qty) * entry,
                "entry_ts": iso(),
                "entry_ms": now,
                "stop": stop,
                "initial_stop": stop,
                "peak": max(entry, stop),
                "trailing": False,
                "score": None,
                "last_candle_open": (now // 60000) * 60000,
                "stop_order_id": o["orderId"],
                "stop_client_id": o["clientOrderId"],
                "order_list_id": o.get("orderListId") if o.get("orderListId", -1) != -1 else None,
                "tp_order_id": None,
                "stop_limit": float(o["price"]),
                "buy_order_id": None,
            }
            rep.append(f"{sym}: found a desk stop order with no record; adopted it as an open position.")
        self.save()
        self.foreign = self._foreign_holdings()
        if self.s.get("start_equity") is None:
            self.s["start_equity"] = round(self.equity({}), 6)
            self.save()
        return rep

    def _foreign_holdings(self):
        """Coins on the account that the desk did NOT buy. They are never touched or counted."""
        mine = {p["base"] for p in self.positions.values()}
        out = []
        try:
            px = {x["symbol"]: float(x["price"]) for x in self.c.request("GET", "/api/v3/ticker/price")}
        except Exception:
            px = {}
        for a, (f, l) in self.balances.items():
            if a in (self.quote, "USDC", "FDUSD") or a in mine or (f + l) <= 0:
                continue
            v = float(f + l) * px.get(a + self.quote, 0.0)
            if v >= 1.0 or not px:
                out.append({"asset": a, "amount": float(f + l), "approx_usdt": round(v, 2)})
        return out

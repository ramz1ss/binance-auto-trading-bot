"""A small in-memory Binance Spot exchange that plugs in where requests.Session goes. It VERIFIES the HMAC signature and API-key header
on signed endpoints and enforces the filters that matter (LOT_SIZE, PRICE_FILTER tick, NOTIONAL, stop-must-be-below-price)."""

import hashlib
import hmac
import itertools
import json
import time
from decimal import ROUND_DOWN, Decimal
from urllib.parse import parse_qsl, urlparse

import requests

KEY, SECRET = "K" * 64, "S" * 64
D = Decimal


class Resp:
    def __init__(self, status, obj, headers=None):
        self.status_code, self._o, self.headers, self.text = status, obj, headers or {}, json.dumps(obj)

    def json(self):
        return self._o


def floor_to(v, step):
    return (D(v) / D(step)).to_integral_value(rounding=ROUND_DOWN) * D(step)


SYMS = {  # symbol: (base, price, step, tick, minNotional)
    "BTCUSDT": ("BTC", D("90000"), D("0.00001"), D("0.01"), D("5")),
    "PEPEUSDT": ("PEPE", D("0.00001234"), D("1"), D("0.00000001"), D("5")),
    "SOLUSDT": ("SOL", D("150"), D("0.001"), D("0.01"), D("5")),
    "SENTUSDT": ("SENT", D("0.05"), D("1"), D("0.0001"), D("5")),
}


class FakeExchange:
    def __init__(self, usdt="7.00", withdrawals=False, ip_restrict=True, can_trade=True, spot_trading=True):
        self.px = {s: v[1] for s, v in SYMS.items()}
        self.bal = {"USDT": [D(usdt), D(0)]}
        self.orders = {}
        self.lists = {}
        self.ids = itertools.count(1000)
        self.trades = []
        self.calls = []
        self.withdrawals, self.ip_restrict, self.can_trade, self.spot_trading = withdrawals, ip_restrict, can_trade, spot_trading
        self.clock_skew_ms = 0
        self.fail = []
        self.drop_response = []
        self.reject_keys = False
        self.blocked = False
        self.fee = D("0.001")
        self.spread = D("0.0001")
        self.whitelist = None  # set of symbols the API key may trade (None = all)

    # ---- test controls
    def fail_next(self, method, path, status=503, code=-1007, msg="Timeout waiting for response from backend server."):
        self.fail.append((method, path, status, code, msg))

    def drop_next_response(self, method, path):
        self.drop_response.append((method, path))

    def posts(self, path="/api/v3/order"):
        return [c for c in self.calls if c[0] == "POST" and c[1] == path]

    def bid(self, s):
        return self.px[s] * (1 - self.spread / 2)

    def ask(self, s):
        return self.px[s] * (1 + self.spread / 2)

    def free(self, a):
        return self.bal.get(a, [D(0), D(0)])[0]

    def total(self, a):
        return sum(self.bal.get(a, [D(0), D(0)]))

    def set_price(self, s, p):
        self.px[s] = D(str(p))
        self._trigger(s)

    # ---- internals
    def _b(self, a):
        return self.bal.setdefault(a, [D(0), D(0)])

    def _err(self, status, code, msg):
        return Resp(status, {"code": code, "msg": msg})

    def _order_view(self, o):
        v = {k: str(x) if isinstance(x, D) else x for k, x in o.items() if not k.startswith("_")}
        v["cummulativeQuoteQty"] = str(o["_quote"])
        v["executedQty"] = str(o["_exec"])
        return v

    def _trigger(self, s):
        last = self.px[s]
        for o in list(self.orders.values()):
            if o["symbol"] != s or o["status"] != "NEW" or o["type"] != "STOP_LOSS_LIMIT":
                continue
            if last <= D(o["stopPrice"]):
                o["_triggered"] = True
            if o.get("_triggered") and self.bid(s) >= D(o["price"]):  # limit leg fills only if the market is at/above the limit price
                self._fill_sell(o, self.bid(s))
            if o.get("_listId") and o["status"] == "FILLED":
                for oo in self.orders.values():
                    if oo.get("_listId") == o["_listId"] and oo is not o and oo["status"] == "NEW":
                        oo["status"] = "EXPIRED"
        for o in list(self.orders.values()):
            if o["symbol"] == s and o["status"] == "NEW" and o["type"] == "LIMIT_MAKER" and last >= D(o["price"]):
                self._fill_sell(o, D(o["price"]))
                for oo in self.orders.values():
                    if oo.get("_listId") == o.get("_listId") and oo is not o and oo["status"] == "NEW":
                        oo["status"] = "EXPIRED"

    def _fill_sell(self, o, price):
        base = SYMS[o["symbol"]][0]
        qty = D(o["origQty"])
        self._b(base)[1] -= qty
        quote = qty * price
        self._b("USDT")[0] += quote * (1 - self.fee)
        o.update(status="FILLED", _exec=qty, _quote=quote)

    def _filters(self, s):
        base, p, step, tick, minn = SYMS[s]
        return [
            {"filterType": "LOT_SIZE", "minQty": str(step), "maxQty": "9000000", "stepSize": str(step)},
            {"filterType": "PRICE_FILTER", "minPrice": str(tick), "maxPrice": "1000000", "tickSize": str(tick)},
            {"filterType": "NOTIONAL", "minNotional": str(minn), "applyMinToMarket": True, "avgPriceMins": 5},
        ]

    # ---- the requests.Session interface
    def request(self, method, url, headers=None, data=None, timeout=None):
        u = urlparse(url)
        path = u.path
        params = dict(parse_qsl(u.query, keep_blank_values=True))
        if data:
            params.update(dict(parse_qsl(data, keep_blank_values=True)))
        headers = headers or {}
        if self.blocked:
            return self._err(451, 0, "Service unavailable from a restricted location")
        if (method, path) in [(m, p) for m, p in self.drop_response]:
            self.drop_response.remove((method, path))
            self._handle(method, path, params, headers, url, data)
            raise requests.ConnectionError("response lost")
        for f in list(self.fail):
            if f[0] == method and f[1] == path:
                self.fail.remove(f)
                return self._err(f[2], f[3], f[4])
        return self._handle(method, path, params, headers, url, data)

    SIGNED = (
        "/api/v3/account",
        "/sapi/v1/account/apiRestrictions",
        "/api/v3/openOrders",
        "/api/v3/order",
        "/api/v3/myTrades",
        "/api/v3/orderList",
        "/api/v3/orderList/oco",
    )

    def _handle(self, method, path, p, headers, url, data):
        self.calls.append((method, path, dict(p)))
        if path in self.SIGNED:
            if self.reject_keys:
                return self._err(401, -2015, "Invalid API-key, IP, or permissions for action.")
            if headers.get("X-MBX-APIKEY") != KEY:
                return self._err(401, -2014, "API-key format invalid.")
            sig = p.pop("signature", None)
            raw = (data if method == "POST" else url.split("?", 1)[1]).rsplit("&signature=", 1)[0]
            if sig != hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest():
                return self._err(400, -1022, "Signature for this request is not valid.")
            if abs(int(p["timestamp"]) - (int(time.time() * 1000) + self.clock_skew_ms)) > int(p.get("recvWindow", 5000)):
                return self._err(400, -1021, "Timestamp for this request is outside of the recvWindow.")
        fn = getattr(self, "h_" + method + path.replace("/", "_"), None)
        if fn is None:
            return self._err(404, -1000, f"unknown route {method} {path}")
        r = fn(p)
        return r if isinstance(r, Resp) else Resp(200, r)

    def h_GET_api_v3_ping(self, p):
        return {}

    def h_GET_api_v3_time(self, p):
        return {"serverTime": int(time.time() * 1000) + self.clock_skew_ms}

    def h_GET_api_v3_exchangeInfo(self, p):
        s = p["symbol"]
        if s not in SYMS:
            return self._err(400, -1121, "Invalid symbol.")
        return {
            "symbols": [
                {
                    "symbol": s,
                    "status": "TRADING",
                    "baseAsset": SYMS[s][0],
                    "quoteAsset": "USDT",
                    "isSpotTradingAllowed": True,
                    "quoteAssetPrecision": 8,
                    "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"],
                    "filters": self._filters(s),
                }
            ]
        }

    def h_GET_api_v3_ticker_bookTicker(self, p):
        row = lambda s: {"symbol": s, "bidPrice": str(self.bid(s)), "bidQty": "10", "askPrice": str(self.ask(s)), "askQty": "10"}
        return row(p["symbol"]) if "symbol" in p else [row(s) for s in SYMS]

    def h_GET_api_v3_ticker_price(self, p):
        return [{"symbol": s, "price": str(v)} for s, v in self.px.items()]

    def h_GET_api_v3_account(self, p):
        return {
            "canTrade": self.can_trade,
            "accountType": "SPOT",
            "permissions": ["SPOT"],
            "balances": [{"asset": a, "free": str(f), "locked": str(l)} for a, (f, l) in self.bal.items() if f + l > 0],
        }

    def h_GET_sapi_v1_account_apiRestrictions(self, p):
        return {
            "ipRestrict": self.ip_restrict,
            "enableReading": True,
            "enableWithdrawals": self.withdrawals,
            "enableSpotAndMarginTrading": self.spot_trading,
        }

    def h_GET_api_v3_openOrders(self, p):
        return [self._order_view(o) for o in self.orders.values() if o["status"] == "NEW" and (not p.get("symbol") or o["symbol"] == p["symbol"])]

    def _find(self, p):
        for o in self.orders.values():
            if o["symbol"] == p.get("symbol") and (str(o["orderId"]) == p.get("orderId") or o["clientOrderId"] == p.get("origClientOrderId")):
                return o

    def h_GET_api_v3_order(self, p):
        o = self._find(p)
        return self._order_view(o) if o else self._err(400, -2013, "Order does not exist.")

    def h_GET_api_v3_myTrades(self, p):
        return [t for t in self.trades if t["symbol"] == p["symbol"]]

    def h_DELETE_api_v3_order(self, p):
        o = self._find(p)
        if not o or o["status"] not in ("NEW", "PARTIALLY_FILLED"):
            return self._err(400, -2011, "Unknown order sent.")
        self._cancel(o)
        return self._order_view(o)

    def _cancel(self, o):
        if o.get("_lock"):
            q = D(o["origQty"])
            b = self._b(SYMS[o["symbol"]][0])
            b[1] -= q
            b[0] += q
        o["status"] = "CANCELED"

    def h_DELETE_api_v3_orderList(self, p):
        ls = [o for o in self.orders.values() if str(o.get("_listId")) == p.get("orderListId") and o["status"] == "NEW"]
        if not ls:
            return self._err(400, -2011, "Unknown order sent.")
        for o in ls:
            self._cancel(o)
        return {"orderListId": int(p["orderListId"])}

    def _new_id(self):
        return next(self.ids)

    def h_POST_api_v3_order(self, p):
        s = p["symbol"]
        if s not in SYMS:
            return self._err(400, -1121, "Invalid symbol.")
        if self.whitelist is not None and s not in self.whitelist:
            return self._err(400, -2010, "Symbol not whitelisted for API key.")
        base, _, step, tick, minn = SYMS[s]
        cid = p.get("newClientOrderId") or f"auto{self._new_id()}"
        if any(o["clientOrderId"] == cid and o["status"] == "NEW" for o in self.orders.values()):
            return self._err(400, -2010, "Duplicate order sent.")
        t = p["type"]
        o = {
            "symbol": s,
            "orderId": self._new_id(),
            "orderListId": -1,
            "clientOrderId": cid,
            "side": p["side"],
            "type": t,
            "status": "NEW",
            "_exec": D(0),
            "_quote": D(0),
            "origQty": p.get("quantity", "0"),
            "price": p.get("price", "0"),
            "stopPrice": p.get("stopPrice", "0"),
        }
        if t == "MARKET" and p["side"] == "BUY":
            quote = D(p["quoteOrderQty"])
            ask = self.ask(s)
            if quote < minn:
                return self._err(400, -1013, "Filter failure: NOTIONAL")
            if self.free("USDT") < quote:
                return self._err(400, -2010, "Account has insufficient balance for requested action.")
            qty = floor_to(quote / ask, step)
            spent = qty * ask
            comm = qty * self.fee
            self._b("USDT")[0] -= spent
            self._b(base)[0] += qty - comm
            o.update(status="FILLED", _exec=qty, _quote=spent, origQty=str(qty))
            fills = [{"price": str(ask), "qty": str(qty), "commission": str(comm), "commissionAsset": base, "tradeId": self._new_id()}]
            self.trades.append(
                {
                    "symbol": s,
                    "orderId": o["orderId"],
                    "price": str(ask),
                    "qty": str(qty),
                    "isBuyer": True,
                    "commission": str(comm),
                    "commissionAsset": base,
                }
            )
            self.orders[o["orderId"]] = o
            v = self._order_view(o)
            v["fills"] = fills
            return v
        if t == "MARKET" and p["side"] == "SELL":
            qty = D(p["quantity"])
            if qty % step != 0 or qty < step:
                return self._err(400, -1013, "Filter failure: LOT_SIZE")
            if self.free(base) < qty:
                return self._err(400, -2010, "Account has insufficient balance for requested action.")
            bid = self.bid(s)
            self._b(base)[0] -= qty
            self._b("USDT")[0] += qty * bid * (1 - self.fee)
            o.update(status="FILLED", _exec=qty, _quote=qty * bid)
            self.orders[o["orderId"]] = o
            v = self._order_view(o)
            v["fills"] = [{"price": str(bid), "qty": str(qty), "commission": str(qty * bid * self.fee), "commissionAsset": "USDT"}]
            return v
        if t == "STOP_LOSS_LIMIT":
            return self._place_stop(o, p, step, tick, minn, base)
        return self._err(400, -1116, "Invalid orderType.")

    def _place_stop(self, o, p, step, tick, minn, base, listId=None):
        qty, price, stop = D(p["quantity"]), D(p["price"]), D(p["stopPrice"])
        if qty % step != 0 or qty < step:
            return self._err(400, -1013, "Filter failure: LOT_SIZE")
        if price % tick != 0 or stop % tick != 0 or price <= 0:
            return self._err(400, -1013, "Filter failure: PRICE_FILTER")
        if qty * price < minn:
            return self._err(400, -1013, "Filter failure: NOTIONAL")
        if p.get("timeInForce") != "GTC":
            return self._err(400, -1102, "Mandatory parameter 'timeInForce' was not sent")
        if stop >= self.px[o["symbol"]]:
            return self._err(400, -2021, "Order would immediately trigger.")
        if self.free(base) < qty:
            return self._err(400, -2010, "Account has insufficient balance for requested action.")
        b = self._b(base)
        b[0] -= qty
        b[1] += qty
        o["_lock"] = True
        o["_listId"] = listId
        self.orders[o["orderId"]] = o
        return self._order_view(o)

    def h_POST_api_v3_orderList_oco(self, p):
        s = p["symbol"]
        base, _, step, tick, minn = SYMS[s]
        lid = self._new_id()
        if self.whitelist is not None and s not in self.whitelist:
            return self._err(400, -2010, "Symbol not whitelisted for API key.")
        for need in ("aboveType", "belowType", "abovePrice", "belowStopPrice", "belowPrice", "belowTimeInForce"):
            if need not in p:
                return self._err(400, -1102, f"Mandatory parameter '{need}' was not sent")
        if p["aboveType"] != "LIMIT_MAKER" or p["belowType"] != "STOP_LOSS_LIMIT":
            return self._err(400, -1116, "Invalid orderType.")
        if not (D(p["abovePrice"]) > self.px[s] > D(p["belowStopPrice"])):
            return self._err(400, -1013, "Invalid price relationship")
        below = {
            "symbol": s,
            "orderId": self._new_id(),
            "orderListId": lid,
            "clientOrderId": p["belowClientOrderId"],
            "side": p["side"],
            "type": "STOP_LOSS_LIMIT",
            "status": "NEW",
            "_exec": D(0),
            "_quote": D(0),
            "origQty": p["quantity"],
            "price": p["belowPrice"],
            "stopPrice": p["belowStopPrice"],
        }
        r = self._place_stop(
            below,
            {"quantity": p["quantity"], "price": p["belowPrice"], "stopPrice": p["belowStopPrice"], "timeInForce": p["belowTimeInForce"]},
            step,
            tick,
            minn,
            base,
            lid,
        )
        if isinstance(r, Resp):
            return r
        above = {
            "symbol": s,
            "orderId": self._new_id(),
            "orderListId": lid,
            "clientOrderId": p["aboveClientOrderId"],
            "side": p["side"],
            "type": "LIMIT_MAKER",
            "status": "NEW",
            "_exec": D(0),
            "_quote": D(0),
            "origQty": p["quantity"],
            "price": p["abovePrice"],
            "stopPrice": "0",
            "_listId": lid,
        }
        self.orders[above["orderId"]] = above
        return {
            "orderListId": lid,
            "symbol": s,
            "orders": [
                {"symbol": s, "orderId": below["orderId"], "clientOrderId": below["clientOrderId"]},
                {"symbol": s, "orderId": above["orderId"], "clientOrderId": above["clientOrderId"]},
            ],
            "orderReports": [self._order_view(below), self._order_view(above)],
        }

    def session(self):
        return self

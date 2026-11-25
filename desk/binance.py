"""Minimal Binance SPOT REST client (official API, HMAC-SHA256 signing). Uses only `requests`.
Secrets are registered with util.register_secret so they are masked in every log line. Nothing here ever prints a key or secret."""

import hashlib
import hmac
import time
from decimal import ROUND_DOWN, Decimal
from urllib.parse import urlencode

import requests

from .util import redact, register_secret


class BinanceError(Exception):
    """Binance answered with an error (or HTTP error). code = Binance error code (e.g. -2010) when present."""

    def __init__(self, status, code, msg):
        self.status, self.code, self.msg = status, code, msg
        super().__init__(f"HTTP {status} code={code}: {msg}")

    @property
    def maybe_executed(self):
        """True when we cannot be sure the request was NOT processed (timeouts / 5xx). Caller must look the order up, never blindly retry."""
        return self.status >= 500 or self.code in (-1006, -1007)

    @property
    def blocked_location(self):
        return self.status == 451

    @property
    def would_trigger(self):
        return self.code == -2021 or "immediately trigger" in (self.msg or "").lower()

    @property
    def not_whitelisted(self):
        """The API key has a symbol whitelist and this symbol is not on it (Binance: -2010 'Symbol not whitelisted for API key')."""
        return "whitelist" in (self.msg or "").lower()

    @property
    def unknown_order(self):
        return self.code in (-2011, -2013)


class NetworkError(Exception):
    """No usable answer (timeout / connection problem). For orders the outcome is UNKNOWN -> maybe_executed."""

    maybe_executed = True


# ---------- decimals: exchange filters must be obeyed exactly, so never format floats directly
def D(x):
    return x if isinstance(x, Decimal) else Decimal(str(x))


def floor_to(value, step):
    """Round value DOWN to a multiple of step (Decimal in, Decimal out). step<=0 -> unchanged."""
    v, s = D(value), D(step)
    if s <= 0:
        return v
    return (v / s).to_integral_value(rounding=ROUND_DOWN) * s


def fmt(x):
    """Decimal -> plain string with no exponent and no trailing zeros ('0.00001230' -> '0.0000123', 100 -> '100')."""
    d = D(x)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


class BinanceClient:
    def __init__(self, api_key, api_secret, base="https://api.binance.com", recv_window=5000, timeout=10, session=None, sleep=time.sleep):
        self.key, self.secret, self.base = api_key, api_secret, base.rstrip("/")
        register_secret(api_key)
        register_secret(api_secret)
        self.recv, self.timeout, self.sleep = int(recv_window), timeout, sleep
        self.s = session or requests.Session()
        self.time_offset_ms = 0

    # ---------------------------------------------------------------- core
    def _sign(self, params):
        p = {k: v for k, v in params.items() if v is not None}
        p["recvWindow"] = self.recv
        p["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
        q = urlencode(p)
        sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        return q + "&signature=" + sig

    def sync_time(self):
        """Measure local-clock error against Binance so signed requests are accepted even if the PC clock drifts."""
        t0 = int(time.time() * 1000)
        srv = self.request("GET", "/api/v3/time")["serverTime"]
        t1 = int(time.time() * 1000)
        self.time_offset_ms = int(srv - (t0 + t1) / 2)
        return self.time_offset_ms

    def request(self, method, path, params=None, signed=False, _resynced=False):
        params = dict(params or {})
        idempotent = method == "GET"
        attempts = 3 if idempotent else 1
        last = None
        for i in range(attempts):
            url = self.base + path
            headers = {"X-MBX-APIKEY": self.key} if signed else {}
            kw = {"headers": headers, "timeout": self.timeout}
            if signed:
                body = self._sign(params)
                if method == "POST":
                    kw["data"] = body
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                else:
                    url += "?" + body
            elif params:
                url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
            try:
                r = self.s.request(method, url, **kw)
            except requests.RequestException as e:
                last = NetworkError(redact(f"{method} {path}: {e.__class__.__name__}: {e}"))
                if i + 1 < attempts:
                    self.sleep(0.5 + i)
                    continue
                raise last from None  # message is already redacted; do not chain the raw exception
            try:
                js = r.json()
            except Exception:
                js = None
            if r.status_code == 200 and js is not None:
                return js
            code = js.get("code") if isinstance(js, dict) else None
            msg = js.get("msg") if isinstance(js, dict) else (r.text or "")[:200]
            err = BinanceError(r.status_code, code, redact(msg))
            if code == -1021 and not _resynced:  # clock out of sync: rejected before processing, safe to retry once
                self.sync_time()
                return self.request(method, path, params, signed, _resynced=True)
            if r.status_code in (418, 429):
                wait = min(int(r.headers.get("Retry-After", 5) or 5), 120)
                self.sleep(wait)
                if idempotent and i + 1 < attempts:
                    last = err
                    continue
                raise err
            if idempotent and r.status_code >= 500 and i + 1 < attempts:
                last = err
                self.sleep(1 + i)
                continue
            raise err
        raise last

    # ---------------------------------------------------------------- public
    def ping(self):
        return self.request("GET", "/api/v3/ping")

    def server_time(self):
        return self.request("GET", "/api/v3/time")["serverTime"]

    def exchange_info(self, symbol):
        return self.request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})["symbols"][0]

    def book_ticker(self, symbol):
        return self.request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})

    # ---------------------------------------------------------------- signed: account
    def account(self):
        return self.request("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)

    def api_restrictions(self):
        return self.request("GET", "/sapi/v1/account/apiRestrictions", signed=True)

    def open_orders(self, symbol=None):
        return self.request("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True)

    def get_order(self, symbol, order_id=None, client_id=None):
        return self.request("GET", "/api/v3/order", {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_id}, signed=True)

    def my_trades(self, symbol, order_id=None, limit=50):
        return self.request("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id, "limit": limit}, signed=True)

    # ---------------------------------------------------------------- signed: trading
    def new_order(self, params):
        return self.request("POST", "/api/v3/order", params, signed=True)

    def new_oco(self, params):
        return self.request("POST", "/api/v3/orderList/oco", params, signed=True)

    def cancel_order(self, symbol, order_id=None, client_id=None):
        return self.request("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_id}, signed=True)

    def cancel_order_list(self, symbol, order_list_id):
        return self.request("DELETE", "/api/v3/orderList", {"symbol": symbol, "orderListId": order_list_id}, signed=True)


# ---------------------------------------------------------------- order construction (pure functions: easy to unit-test)
def market_buy_params(symbol, quote_usdt, client_id):
    """Real market BUY spending exactly `quote_usdt` (quoteOrderQty). FULL response gives fills + commissions."""
    return {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": fmt(quote_usdt),
        "newClientOrderId": client_id,
        "newOrderRespType": "FULL",
    }


def market_sell_params(symbol, qty, client_id):
    return {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": fmt(qty), "newClientOrderId": client_id, "newOrderRespType": "FULL"}


def stop_limit_params(symbol, qty, stop_price, limit_price, client_id):
    """Protective SELL STOP_LOSS_LIMIT: triggers when price <= stop_price, then rests as a limit sell at limit_price (< stop_price)."""
    return {
        "symbol": symbol,
        "side": "SELL",
        "type": "STOP_LOSS_LIMIT",
        "timeInForce": "GTC",
        "quantity": fmt(qty),
        "stopPrice": fmt(stop_price),
        "price": fmt(limit_price),
        "newClientOrderId": client_id,
        "newOrderRespType": "RESULT",
    }


def oco_params(symbol, qty, stop_price, limit_price, take_profit_price, stop_client_id, tp_client_id, list_client_id):
    """Protective SELL OCO (new /orderList/oco API): above = LIMIT_MAKER take-profit, below = STOP_LOSS_LIMIT."""
    return {
        "symbol": symbol,
        "side": "SELL",
        "quantity": fmt(qty),
        "aboveType": "LIMIT_MAKER",
        "abovePrice": fmt(take_profit_price),
        "aboveClientOrderId": tp_client_id,
        "belowType": "STOP_LOSS_LIMIT",
        "belowStopPrice": fmt(stop_price),
        "belowPrice": fmt(limit_price),
        "belowTimeInForce": "GTC",
        "belowClientOrderId": stop_client_id,
        "listClientOrderId": list_client_id,
        "newOrderRespType": "RESULT",
    }


# ---------------------------------------------------------------- exchange filters
class SymbolRules:
    """Parsed exchangeInfo for one symbol: tick/step/min-notional etc. All Decimal."""

    def __init__(self, info):
        f = {x["filterType"]: x for x in info.get("filters", [])}
        self.symbol = info["symbol"]
        self.base = info.get("baseAsset")
        self.quote = info.get("quoteAsset")
        self.status = info.get("status")
        self.order_types = set(info.get("orderTypes", []))
        self.spot_allowed = info.get("isSpotTradingAllowed", True)
        lot = f.get("LOT_SIZE", {})
        self.step = D(lot.get("stepSize", "0"))
        self.min_qty = D(lot.get("minQty", "0"))
        self.max_qty = D(lot.get("maxQty", "9e18"))
        pf = f.get("PRICE_FILTER", {})
        self.tick = D(pf.get("tickSize", "0"))
        self.min_price = D(pf.get("minPrice", "0"))
        n = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
        self.min_notional = D(n.get("minNotional", "5"))
        self.apply_min_to_market = bool(n.get("applyMinToMarket", True))
        self.quote_precision = int(info.get("quoteAssetPrecision", info.get("quotePrecision", 8)))

    def tradable_problem(self):
        """None if we can BUY (market) and protect (stop-limit) this symbol, else a plain-English reason."""
        if self.status != "TRADING":
            return f"status is {self.status}"
        if not self.spot_allowed:
            return "spot trading not allowed"
        for t in ("MARKET", "STOP_LOSS_LIMIT"):
            if t not in self.order_types:
                return f"order type {t} not supported"
        return None

    def qty_down(self, q):
        return floor_to(q, self.step)

    def price_down(self, p):
        v = floor_to(p, self.tick)
        return v

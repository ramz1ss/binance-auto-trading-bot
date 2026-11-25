import re
import time
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests


class Data:
    def __init__(self, cfg):
        d = cfg["data"]
        self.b = d["binance_base"]
        self.okx = d["okx_base"]
        self.gate = d["gate_base"]
        self.t = d["timeout_sec"]
        self.fapi = d.get("binance_futures_base", "")  # set in live mode only
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (crypto-desk)"
        self._info = {}

    def get(self, url, params=None, retries=3):
        err = None
        for i in range(retries):
            try:
                r = self.s.get(url, params=params, timeout=self.t)
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(1 + i)
                    err = f"HTTP {r.status_code}"
                    continue
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                err = str(e)
                time.sleep(0.5 + i)
        raise RuntimeError(f"GET {url} failed: {err}")

    # ---- Binance public spot (data-api.binance.vision)
    def tickers_24h(self):
        return self.get(f"{self.b}/api/v3/ticker/24hr").json()

    def book_tickers(self):
        return {x["symbol"]: x for x in self.get(f"{self.b}/api/v3/ticker/bookTicker").json()}

    def book_ticker(self, sym):
        return self.get(f"{self.b}/api/v3/ticker/bookTicker", {"symbol": sym}).json()

    def klines(self, sym, interval, limit=100, start=None):
        p = {"symbol": sym, "interval": interval, "limit": limit}
        if start:
            p["startTime"] = int(start)
        return self.get(f"{self.b}/api/v3/klines", p).json()

    def depth(self, sym, limit=1000):
        return self.get(f"{self.b}/api/v3/depth", {"symbol": sym, "limit": limit}).json()

    def symbol_info(self, sym):
        if sym not in self._info:
            self._info[sym] = self.get(f"{self.b}/api/v3/exchangeInfo", {"symbol": sym}).json()["symbols"][0]
        return self._info[sym]

    # ---- Funding: OKX bulk (primary), Gate bulk (fallback). Rates normalised to per-8h.
    def funding_map(self):
        out, health = {}, {}

        def put(base, rec):  # several sources may list a coin: keep the most extreme rate (strictest filter)
            old = out.get(base)
            if old is None or abs(rec["rate_8h"]) > abs(old["rate_8h"]):
                out[base] = rec

        if self.fapi:  # live mode: Binance's own futures funding (premiumIndex)
            try:
                try:
                    iv = {x["symbol"]: float(x["fundingIntervalHours"]) for x in self.get(f"{self.fapi}/fapi/v1/fundingInfo", retries=1).json()}
                except Exception:
                    iv = {}
                n = 0
                for x in self.get(f"{self.fapi}/fapi/v1/premiumIndex").json():
                    s = x["symbol"]
                    if not s.endswith("USDT") or x.get("lastFundingRate") in (None, ""):
                        continue
                    hrs = iv.get(s, 8.0) or 8.0
                    put(re.sub(r"^10{3,}", "", s[:-4]), {"rate_8h": float(x["lastFundingRate"]) * 8 / hrs, "source": "binance", "interval_h": hrs})
                    n += 1
                health["binance_fapi"] = f"ok ({n} perps)"
            except Exception as e:
                health["binance_fapi"] = f"error: {str(e)[:80]}"
        try:
            for x in self.get(f"{self.okx}/api/v5/public/funding-rate", {"instId": "ANY"}).json()["data"]:
                iid = x["instId"]
                if not iid.endswith("-USDT-SWAP") or x["fundingRate"] == "":
                    continue
                try:
                    hrs = (int(x["nextFundingTime"]) - int(x["fundingTime"])) / 3.6e6
                    if hrs <= 0 or hrs > 24:
                        hrs = 8
                except Exception:
                    hrs = 8
                put(iid.split("-")[0], {"rate_8h": float(x["fundingRate"]) * 8 / hrs, "source": "okx", "interval_h": hrs})
            health["okx"] = "ok"
        except Exception as e:
            health["okx"] = f"error: {e}"
        try:
            n = 0
            for c in self.get(f"{self.gate}/api/v4/futures/usdt/contracts").json():
                base = c["name"].split("_")[0]
                if base in out or c.get("in_delisting"):
                    continue
                hrs = float(c.get("funding_interval", 28800)) / 3600
                out[base] = {"rate_8h": float(c["funding_rate"]) * 8 / hrs, "source": "gate", "interval_h": hrs}
                n += 1
            health["gate"] = f"ok (+{n} fallback)"
        except Exception as e:
            health["gate"] = f"error: {e}"
        return out, health

    # ---- News RSS
    def news(self, feeds, lookback_h):
        items, health = [], {}
        cutoff = time.time() - lookback_h * 3600
        for u in feeds:
            try:
                root = ET.fromstring(self.get(u, retries=2).content)
                n = 0
                for it in root.iter("item"):
                    title = (it.findtext("title") or "").strip()
                    try:
                        ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
                    except Exception:
                        continue
                    if ts >= cutoff:
                        items.append({"title": title, "ts": ts, "src": u})
                        n += 1
                health[u] = f"ok ({n} recent)"
            except Exception as e:
                health[u] = f"error: {str(e)[:80]}"
        return items, health

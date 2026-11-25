import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

NAMES = {
    "BTC": ["bitcoin"],
    "ETH": ["ethereum", "ether"],
    "BNB": ["bnb", "binance"],
    "SOL": ["solana"],
    "XRP": ["ripple"],
    "DOGE": ["dogecoin"],
    "ADA": ["cardano"],
    "AVAX": ["avalanche"],
    "LINK": ["chainlink"],
    "TRX": ["tron"],
    "DOT": ["polkadot"],
    "LTC": ["litecoin"],
    "SHIB": ["shiba"],
    "TON": ["toncoin"],
    "NEAR": ["near protocol"],
    "SUI": ["sui network"],
    "UNI": ["uniswap"],
    "AAVE": ["aave"],
    "ATOM": ["cosmos"],
    "BCH": ["bitcoin cash"],
    "PEPE": ["pepe"],
    "HBAR": ["hedera"],
    "XLM": ["stellar"],
    "APT": ["aptos"],
    "ARB": ["arbitrum"],
    "OP": ["optimism"],
    "FIL": ["filecoin"],
    "ETC": ["ethereum classic"],
    "ICP": ["internet computer"],
    "ENA": ["ethena"],
    "WLD": ["worldcoin"],
    "ZEC": ["zcash"],
    "XMR": ["monero"],
    "TAO": ["bittensor"],
    "LDO": ["lido"],
}


class Filter:
    """Agent 2: kills candidates failing any check. All checks run so every kill logs every failed reason."""

    def __init__(self, cfg, data, logger):
        self.cfg = cfg
        self.c = cfg["filter"]
        self.d = data
        self.lg = logger
        self.workers = cfg["data"]["workers"]
        self.kw = re.compile(r"\b(" + "|".join(re.escape(k) for k in self.c["news_risk_keywords"]) + r")\b", re.I)
        self.health = {}

    def _events(self):
        p = os.path.join(self.cfg["_root"], self.c["events_file"])  # re-read every cycle: edits to events.json apply live
        try:
            return json.load(open(p, encoding="utf-8")).get("events", [])
        except Exception as e:
            self.health["events"] = f"error: {e}"
            return []

    def run(self, cands, cycle):
        c = self.c
        funding, fh = self.d.funding_map()
        self.health["funding"] = fh
        news, nh = self.d.news(c["news_feeds"], c["news_lookback_hours"])
        self.health["news"] = nh
        news_ok = any(v.startswith("ok") for v in nh.values())
        if not news_ok:
            self.lg.skip(cycle, "news_risk", f"all news feeds unavailable: {nh}")
        if not funding:
            self.lg.skip(cycle, "funding", f"no funding source available: {fh}")
        events = self._events()
        now = time.time()
        # Event window = [time - block_before_hours (default event_horizon_hours), time + block_after_hours (default 0)]
        upcoming = []
        for e in events:
            try:
                t = datetime.fromisoformat(e["time_utc"].replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            start = t - float(e.get("block_before_hours", c["event_horizon_hours"])) * 3600
            end = t + float(e.get("block_after_hours", 0)) * 3600
            if start <= now <= end:
                upcoming.append({**e, "ts": t})
        self.health["events"] = f"{len(events)} in calendar, {len(upcoming)} blocking now"

        def check(cand):
            fails = []

            def res(name, ok, value, thr, detail=""):
                rec = {"check": name, "value": value, "threshold": thr, "detail": detail}
                cand.checks[name] = {"ok": ok, "value": value, "threshold": thr}
                if not ok:
                    fails.append(rec)

            # 1 spread
            if cand.bid > 0 and cand.ask > 0:
                mid = (cand.bid + cand.ask) / 2
                sp = (cand.ask - cand.bid) / mid * 100
                res("spread", sp <= c["max_spread_pct"], round(sp, 4), c["max_spread_pct"], "pct (bookTicker)")
            else:
                res("spread", False, None, c["max_spread_pct"], "no bookTicker quote")
            # 2 funding
            f = funding.get(cand.base)
            if f:
                res(
                    "funding",
                    abs(f["rate_8h"]) <= c["max_abs_funding_8h"],
                    round(f["rate_8h"], 6),
                    c["max_abs_funding_8h"],
                    f"per-8h via {f['source']}",
                )
            elif c["funding_missing_policy"] == "kill":
                res("funding", False, None, c["max_abs_funding_8h"], "no perp listing on OKX/Gate")
            else:
                cand.checks["funding"] = {"ok": True, "skipped": True}
                self.lg.skip(cycle, "funding", "no perp listing on OKX/Gate; check skipped", cand.symbol)
            # 3 volume vs 10d avg
            if cand.avg_quote_vol_10d > 0:
                r = cand.quote_vol_24h / cand.avg_quote_vol_10d
                res("volume", r >= c["min_volume_ratio"], round(r, 3), c["min_volume_ratio"], "24h quote vol / 10d avg daily")
            else:
                res("volume", False, None, c["min_volume_ratio"], "no 10d history")
            # 4 depth within 1% of mid
            try:
                dp = self.d.depth(cand.symbol, 1000)
                mid = (cand.bid + cand.ask) / 2 if cand.bid else cand.price
                lo, hi = mid * (1 - c["depth_pct"] / 100), mid * (1 + c["depth_pct"] / 100)
                bids = sum(float(p) * float(q) for p, q in dp["bids"] if float(p) >= lo)
                asks = sum(float(p) * float(q) for p, q in dp["asks"] if float(p) <= hi)
                covered = float(dp["bids"][-1][0]) <= lo and float(dp["asks"][-1][0]) >= hi
                d = min(bids, asks)
                ok = d >= c["min_depth_usd"] or (not covered and d >= c["min_depth_usd"] * 0.25)  # truncated book on very dense pairs
                res(
                    "depth",
                    ok,
                    round(d),
                    c["min_depth_usd"],
                    f"min(bid,ask) USD within {c['depth_pct']}%" + ("" if covered else " (book truncated at 1000 levels, lower bound)"),
                )
            except Exception as e:
                cand.checks["depth"] = {"ok": True, "skipped": True}
                self.lg.skip(cycle, "depth", f"depth fetch failed: {e}", cand.symbol)
            # 5 news risk
            if news_ok:
                pats = [re.compile(r"\b" + re.escape(n) + r"\b", re.I) for n in NAMES.get(cand.base, [])]
                if len(cand.base) >= 3:
                    pats.append(re.compile(r"\b" + re.escape(cand.base) + r"\b"))  # ticker, case-sensitive
                hits = [n["title"] for n in news if any(p.search(n["title"]) for p in pats) and self.kw.search(n["title"])]
                res("news", not hits, len(hits), 0, "; ".join(h[:110] for h in hits[:2]))
            else:
                cand.checks["news"] = {"ok": True, "skipped": True}
            # 6 unlock / macro events (macro = all symbols; unlock = listed base tickers)
            ev = [e for e in upcoming if e["type"] == "macro" or cand.base in e.get("symbols", [])]
            res("event", not ev, len(ev), 0, "; ".join(f"{e['type']}:{e['name']}@{e['time_utc']}" for e in ev))
            return cand, fails

        with ThreadPoolExecutor(self.workers) as ex:
            results = list(ex.map(check, cands))
        survivors = []
        for cand, fails in results:
            if fails:
                self.lg.kill(cycle, "filter", cand.symbol, fails)
            else:
                survivors.append(cand)
        return survivors

import re
from concurrent.futures import ThreadPoolExecutor

from .config import allowed_symbols
from .models import Candidate


class Watcher:
    """Agent 1: scans BTC/ETH/BNB + top USDT alts by 24h volume and prepares candidates (prices, books, 1h & 10d volume history)."""

    def __init__(self, cfg, data, logger):
        self.c = cfg["watcher"]
        self.workers = cfg["data"]["workers"]
        self.d = data
        self.lg = logger
        self.allowed = allowed_symbols(cfg)  # None = no restriction
        self._warned_missing = False

    def universe(self, tickers):
        ex = set(self.c["exclude_bases"])
        majors = self.c["majors"]
        rows = []
        rx = re.compile(self.c.get("exclude_regex", "$^"))
        allow = self.allowed
        for t in tickers:
            s = t["symbol"]
            if allow is not None and s not in allow:
                continue  # allowlist: nothing else is ever considered (majors only if listed)
            if not s.endswith("USDT"):
                continue
            base = s[:-4]
            if base in ex or base.endswith(("UP", "DOWN", "BULL", "BEAR")) and len(base) > 4:
                continue
            if allow is None and s not in majors and rx.match(base):
                continue  # explicit allowlist overrides the tokenized-stock regex
            qv = float(t["quoteVolume"])
            if qv <= 0:
                continue
            last = float(t["lastPrice"])
            rng = (float(t["highPrice"]) - float(t["lowPrice"])) / last if last else 0
            if 0.97 <= last <= 1.03 and rng < 0.01 and s not in majors:
                continue  # stablecoin-like (pegged, <1% 24h range)
            rows.append((qv, s, t))
        rows.sort(reverse=True)
        if allow is not None:
            if not self._warned_missing:
                missing = sorted(allow - {s for _, s, _ in rows})
                if missing and self.lg:
                    self.lg.log.info(
                        "allowlist: %d of %d symbols not tradable/listed right now (not watched): %s", len(missing), len(allow), ", ".join(missing)
                    )
                self._warned_missing = True
            return [(s, t) for qv, s, t in rows]  # ALL allowed coins, ranked by 24h volume (top_alts / min volume not applied)
        sel = [(s, t) for qv, s, t in rows if s in majors]
        sel += [(s, t) for qv, s, t in rows if s not in majors and qv >= self.c["min_alt_quote_volume"]][: self.c["top_alts"]]
        return sel

    def _prep(self, args):
        i, (s, t), bt = args
        base = s[:-4]
        c = Candidate(
            symbol=s,
            base=base,
            rank=i,
            price=float(t["lastPrice"]),
            quote_vol_24h=float(t["quoteVolume"]),
            change_24h_pct=float(t["priceChangePercent"]),
        )
        b = bt.get(s)
        if b:
            c.bid, c.ask, c.bid_qty, c.ask_qty = float(b["bidPrice"]), float(b["askPrice"]), float(b["bidQty"]), float(b["askQty"])
        d = self.d.klines(s, "1d", 12)[:-1][-10:]  # drop the still-forming day
        c.avg_quote_vol_10d = sum(float(k[7]) for k in d) / len(d) if d else 0.0
        h = self.d.klines(s, "1h", 100)[:-1]  # closed hourly candles
        c.closes_1h = [float(k[4]) for k in h]
        c.qvols_1h = [float(k[7]) for k in h]
        return c

    def scan(self):
        tickers = self.d.tickers_24h()
        bt = self.d.book_tickers()
        sel = self.universe(tickers)
        with ThreadPoolExecutor(self.workers) as ex:
            out = list(ex.map(self._prep, [(i + 1, x, bt) for i, x in enumerate(sel)]))
        return out

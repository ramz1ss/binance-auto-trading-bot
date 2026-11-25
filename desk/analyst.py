from .util import clip, ema, rsi


class Analyst:
    """Agent 3: scores survivors 0..1 = w_m*momentum + w_t*trend + w_v*volume. Score < min_score dies."""

    def __init__(self, cfg, logger):
        self.c = cfg["analyst"]
        self.lg = logger

    def score(self, c):
        cl, qv = c.closes_1h, c.qvols_1h
        if len(cl) < 60:
            return 0.0, {"error": "insufficient history"}
        r6, r24 = cl[-1] / cl[-7] - 1, cl[-1] / cl[-25] - 1
        rs = rsi(cl)
        mom = 0.5 * clip(0.5 + r6 / 0.08) + 0.5 * clip(0.5 + r24 / 0.20)
        if rs > 75:
            mom *= 0.8  # overbought penalty
        e20, e50 = ema(cl, 20), ema(cl, 50)
        t1 = 1.0 if cl[-1] > e20[-1] else 0.0
        t2 = 1.0 if e20[-1] > e50[-1] else 0.0
        t3 = clip(0.5 + (e50[-1] / e50[-13] - 1) / 0.02)
        trend = 0.35 * t1 + 0.35 * t2 + 0.30 * t3
        ratio = c.quote_vol_24h / c.avg_quote_vol_10d if c.avg_quote_vol_10d else 0
        rr = (sum(qv[-6:]) / 6) / (sum(qv[-24:]) / 24) if sum(qv[-24:]) else 0
        vol = 0.5 * clip(ratio / 2) + 0.5 * clip(rr / 2)
        s = self.c["w_momentum"] * mom + self.c["w_trend"] * trend + self.c["w_volume"] * vol
        f = {
            "momentum": round(mom, 3),
            "trend": round(trend, 3),
            "volume": round(vol, 3),
            "r6h_pct": round(r6 * 100, 2),
            "r24h_pct": round(r24 * 100, 2),
            "rsi14": round(rs, 1),
            "vol_ratio_10d": round(ratio, 2),
            "vol_6h_vs_24h": round(rr, 2),
        }
        return round(s, 4), f

    def run(self, survivors, cycle):
        keep = []
        for c in survivors:
            c.score, c.factors = self.score(c)
            if c.score < self.c["min_score"]:
                self.lg.kill(
                    cycle,
                    "analyst",
                    c.symbol,
                    [{"check": "score", "value": c.score, "threshold": self.c["min_score"], "detail": json_short(c.factors)}],
                    score=c.score,
                )
            else:
                keep.append(c)
        return sorted(keep, key=lambda x: -x.score)


def json_short(f):
    return ", ".join(f"{k}={v}" for k, v in f.items())

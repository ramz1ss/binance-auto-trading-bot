class Risk:
    """Agent 5: sets the -8% stop BEFORE entry (approve), then manages stops: trailing 5% below peak once the activation level (trail_activate_pct, default +10%) is reached.
    Stop ratchets up only. Candle order assumption is conservative: low is hit before high within a 1m candle."""

    def __init__(self, cfg, data, logger):
        r = cfg["risk"]
        self.sl = r["stop_loss_pct"] / 100
        self.act = r["trail_activate_pct"] / 100
        self.trail = r["trail_pct"] / 100
        self.d = data
        self.lg = logger

    def stop_for(self, entry):
        return entry * (1 - self.sl)  # initial stop level for an actual fill price

    def trail_for(self, peak):
        return peak * (1 - self.trail)  # trailing stop level for a peak

    def live_update(self, pos, bid, min_step=0.0025, high_since=None):
        """LIVE: Binance holds the real stop order. This only tracks the peak and says where the stop should move.
        Returns the new stop price if a replacement is due (trailing active AND the new stop is at least min_step above the current one), else None."""
        high = high_since
        if high is None:
            high = 0.0
            try:
                ks = self.d.klines(pos["symbol"], "1m", 1000, start=pos["last_candle_open"] + 60000)
            except Exception as e:
                self.lg.log.warning("kline peak fetch failed %s: %s", pos["symbol"], e)
                ks = []
            import time

            for k in ks:
                high = max(high, float(k[2]))
                if int(k[6]) < time.time() * 1000:
                    pos["last_candle_open"] = int(k[0])
        if bid:
            high = max(high, bid)
        if high > pos["peak"]:
            pos["peak"] = high
        if pos["peak"] >= pos["entry"] * (1 + self.act):
            pos["trailing"] = True
            cand = self.trail_for(pos["peak"])
            if cand > pos["stop"] * (1 + min_step):
                return cand
        return None

    def approve(self, order):
        order.stop_price = order.ref_ask * (1 - self.sl)
        order.risk_note = f"initial stop -{self.sl * 100:.0f}% = {order.stop_price:.8g}"
        return order.stop_price > 0

    def _apply(self, pos, high, low, opn):
        """Mutates pos; returns exit price if stopped within this candle else None."""
        if low <= pos["stop"]:
            return min(pos["stop"], opn)  # gap-through fills at open
        if high > pos["peak"]:
            pos["peak"] = high
        if pos["peak"] >= pos["entry"] * (1 + self.act):
            pos["trailing"] = True
            pos["stop"] = max(pos["stop"], pos["peak"] * (1 - self.trail))
        return None

    def monitor(self, pos, bid):
        """Returns (exit_price, reason) or None. Updates pos in place."""
        try:
            ks = self.d.klines(pos["symbol"], "1m", 1000, start=pos["last_candle_open"] + 60000)
        except Exception as e:
            self.lg.log.warning("kline monitor fetch failed %s: %s", pos["symbol"], e)
            ks = []
        import time

        for k in ks:
            x = self._apply(pos, float(k[2]), float(k[3]), float(k[1]))
            if x is not None:
                return x, ("trailing_stop" if pos["trailing"] else "stop_loss")
            if int(k[6]) < time.time() * 1000:
                pos["last_candle_open"] = int(k[0])  # only advance on closed candles
        if bid > pos["peak"]:
            pos["peak"] = bid
            if pos["peak"] >= pos["entry"] * (1 + self.act):
                pos["trailing"] = True
                pos["stop"] = max(pos["stop"], pos["peak"] * (1 - self.trail))
        if bid <= pos["stop"]:
            return bid, ("trailing_stop" if pos["trailing"] else "stop_loss")
        return None

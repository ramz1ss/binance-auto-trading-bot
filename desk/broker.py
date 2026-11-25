import json
import os
import time

from .util import atomic_write_json, iso


class PaperBroker:
    live = False
    """Simulated account. Buys fill at ask, sells at bid, fee 0.1% per side. State persists in state/state.json."""

    def __init__(self, cfg):
        self.fee = cfg["executor"]["fee_rate"]
        self.path = os.path.join(cfg["_data"], "state", "state.json")
        self.start = cfg["start_balance"]
        self.s = {
            "cash": self.start,
            "positions": {},
            "realized_pnl": 0.0,
            "fees_paid": 0.0,
            "closed": 0,
            "wins": 0,
            "last_exit": {},
            "started": iso(),
        }
        if os.path.exists(self.path):
            self.s.update(json.load(open(self.path, encoding="utf-8")))

    def save(self):
        atomic_write_json(self.path, self.s)

    @property
    def cash(self):
        return self.s["cash"]

    @property
    def positions(self):
        return self.s["positions"]

    def equity(self, bids):
        return self.s["cash"] + sum(p["qty"] * bids.get(sym, p["entry"]) for sym, p in self.positions.items())

    def buy(self, symbol, qty, ask, stop, score):
        notional = qty * ask
        fee = notional * self.fee
        if notional + fee > self.s["cash"] + 1e-12:
            raise ValueError("insufficient cash")
        self.s["cash"] -= notional + fee
        self.s["fees_paid"] += fee
        self.positions[symbol] = {
            "symbol": symbol,
            "qty": qty,
            "entry": ask,
            "entry_fee": fee,
            "entry_ts": iso(),
            "entry_ms": int(time.time() * 1000),
            "stop": stop,
            "initial_stop": stop,
            "peak": ask,
            "trailing": False,
            "score": score,
            "last_candle_open": (int(time.time() * 1000) // 60000) * 60000,
        }
        self.save()
        return notional, fee

    def sell(self, symbol, bid, reason):
        p = self.positions.pop(symbol)
        notional = p["qty"] * bid
        fee = notional * self.fee
        self.s["cash"] += notional - fee
        self.s["fees_paid"] += fee
        pnl = notional - fee - (p["qty"] * p["entry"] + p["entry_fee"])
        self.s["realized_pnl"] += pnl
        self.s["closed"] += 1
        self.s["wins"] += pnl > 0
        self.s["last_exit"][symbol] = time.time()
        self.save()
        return notional, fee, pnl, pnl / (p["qty"] * p["entry"] + p["entry_fee"]) * 100

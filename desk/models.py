from dataclasses import dataclass, field


@dataclass
class Candidate:  # Watcher -> Filter -> Analyst
    symbol: str
    base: str
    rank: int
    price: float
    quote_vol_24h: float
    change_24h_pct: float
    avg_quote_vol_10d: float = 0.0
    closes_1h: list = field(default_factory=list)
    qvols_1h: list = field(default_factory=list)
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    checks: dict = field(default_factory=dict)  # filled by Filter
    score: float = None  # filled by Analyst
    factors: dict = field(default_factory=dict)


@dataclass
class Order:  # Executor -> Risk -> Broker
    symbol: str
    qty: float
    ref_ask: float
    notional: float
    score: float
    stop_price: float = None  # set by Risk before entry
    risk_note: str = ""

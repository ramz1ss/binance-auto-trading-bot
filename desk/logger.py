import csv
import json
import logging
import os
import threading

from .util import atomic_write_json, iso, redact

TRADE_FIELDS = [
    "ts",
    "event",
    "symbol",
    "side",
    "qty",
    "price",
    "notional",
    "fee",
    "pnl",
    "pnl_pct",
    "reason",
    "score",
    "stop",
    "cash_after",
    "equity_after",
]


class Logger:
    """Agent 6: Logger. Writes kills.jsonl, trades.jsonl/csv, cycles.jsonl, skips.jsonl, status.json."""

    def __init__(self, cfg):
        self.dir = os.path.join(cfg["_data"], "logs")
        os.makedirs(self.dir, exist_ok=True)
        self.state_dir = os.path.join(cfg["_data"], "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.lock = threading.Lock()
        self.status_path = os.path.join(cfg["_data"], "status.json")
        self.counters_path = os.path.join(self.state_dir, "counters.json")
        self.counters = {"cycles": 0, "candidates_seen": 0, "kills_total": 0, "kills_by_check": {}, "kills_by_stage": {}, "skips_total": 0}
        if os.path.exists(self.counters_path):
            try:
                self.counters.update(json.load(open(self.counters_path, encoding="utf-8")))
            except Exception:
                pass

        class _Redact(logging.Filter):  # keys/secrets can never end up in a log file
            def filter(self, rec):
                rec.msg = redact(rec.getMessage())
                rec.args = ()
                return True

        self.log = logging.getLogger("desk")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        for h in list(self.log.handlers):
            self.log.removeHandler(h)
            h.close()
        fh = logging.FileHandler(os.path.join(self.dir, "desk.log"), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        fh.addFilter(_Redact())
        self.log.addHandler(fh)
        self.log.addFilter(_Redact())
        self.csv_path = os.path.join(self.dir, "trades.csv")
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, TRADE_FIELDS).writeheader()

    def _append(self, name, rec):
        with self.lock, open(os.path.join(self.dir, name), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def kill(self, cycle, stage, symbol, reasons, score=None):
        """reasons: list of {check, value, threshold, detail}. One record per killed candidate per cycle."""
        self._append("kills.jsonl", {"ts": iso(), "cycle": cycle, "stage": stage, "symbol": symbol, "score": score, "reasons": reasons})
        with self.lock:
            c = self.counters
            c["kills_total"] += 1
            c["kills_by_stage"][stage] = c["kills_by_stage"].get(stage, 0) + 1
            for r in reasons:
                c["kills_by_check"][r["check"]] = c["kills_by_check"].get(r["check"], 0) + 1

    def skip(self, cycle, check, detail, symbol=None):
        self._append("skips.jsonl", {"ts": iso(), "cycle": cycle, "check": check, "symbol": symbol, "detail": detail})
        with self.lock:
            self.counters["skips_total"] += 1

    def trade(self, rec):
        rec = {"ts": iso(), **rec}
        self._append("trades.jsonl", rec)
        with self.lock, open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, TRADE_FIELDS, extrasaction="ignore").writerow(rec)
        self.log.info("TRADE %s", json.dumps(rec, default=str))

    def cycle(self, rec):
        self._append("cycles.jsonl", {"ts": iso(), **rec})
        with self.lock:
            self.counters["cycles"] += 1
            self.counters["candidates_seen"] += rec.get("candidates", 0)
        self.save_counters()

    def save_counters(self):
        with self.lock:
            atomic_write_json(self.counters_path, self.counters)

    def status(self, obj):
        obj = {**obj, "counters": dict(self.counters)}
        atomic_write_json(self.status_path, obj)

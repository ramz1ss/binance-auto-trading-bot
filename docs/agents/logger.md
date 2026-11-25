# Agent 6: Logger

**Code:** [`desk/logger.py`](../../desk/logger.py)

The Logger makes the desk auditable. All files are plain text: JSON Lines (one JSON object per line), CSV and a regular log.
They live in `paper/logs/` (paper) or `logs/` (live). See the [file table](../architecture.md#files).

## Records

**Kill** (`kills.jsonl`): one per rejected candidate per cycle.

```json
{"ts": "2026-01-01T12:00:00Z", "cycle": 42, "stage": "filter", "symbol": "ABCUSDT", "score": null,
 "reasons": [{"check": "spread", "value": 0.31, "threshold": 0.1, "detail": "pct (bookTicker)"}]}
```

**Trade** (`trades.jsonl` and `trades.csv`): every entry and exit. CSV columns:
`ts, event, symbol, side, qty, price, notional, fee, pnl, pnl_pct, reason, score, stop, cash_after, equity_after`.

**Cycle** (`cycles.jsonl`): counts per stage, the watched symbols, ranked candidates with their factors, and the entry, if any.

**Skip** (`skips.jsonl`): a check that could not run, or an entry deliberately not taken (for example while paused).

**Status** (`status.json`): rewritten atomically every tick. The dashboard and `status.py` read it.

## Secret redaction

API keys are registered at load time, and a logging filter replaces them in every message. Tracebacks are redacted before
they are written. Logs can still contain symbols, amounts and order ids: treat them as private and do not post them publicly.

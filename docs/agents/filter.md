# Agent 2: Filter

**Code:** [`desk/filter.py`](../../desk/filter.py) · **Config:** `[filter]`

The Filter rejects ("kills") any candidate that fails **any** check. All checks run for every candidate, so each rejection in
`logs/kills.jsonl` lists **every** failed reason with its value and threshold.

| # | Check | Passes when | Setting |
|---|-------|-------------|---------|
| 1 | **Spread** | `(ask - bid) / mid ≤ max_spread_pct` (percent) | `max_spread_pct = 0.10` |
| 2 | **Funding** | absolute perpetual funding rate, normalised to 8h, `≤ max_abs_funding_8h` | `max_abs_funding_8h = 0.0005` |
| 3 | **Volume** | 24h quote volume / 10-day average daily volume `≥ min_volume_ratio` | `min_volume_ratio = 1.0` |
| 4 | **Depth** | `min(bid side, ask side)` notional within `depth_pct` of mid `≥ min_depth_usd` | `depth_pct = 1.0`, `min_depth_usd = 100_000` |
| 5 | **News** | no headline from the last `news_lookback_hours` mentions the coin **and** a risk keyword | `news_feeds`, `news_risk_keywords` |
| 6 | **Events** | no unlock for this coin and no macro event inside its blocking window | `events_file`, `event_horizon_hours` |

## Details

- **Funding sources.** OKX first, Gate as fallback, plus Binance Futures `premiumIndex` in live mode. When several sources list a coin,
  the most extreme rate wins (strictest). If no source lists a coin, `funding_missing_policy` decides: `"pass"` (log a skip) or `"kill"`.
- **Depth.** Uses up to 1000 levels. On very dense books the window may not be fully covered; then 25% of `min_depth_usd` is accepted
  and the log notes that the book was truncated.
- **News.** Headlines are matched against the ticker (case-sensitive, 3+ letters) and known coin names (for example "solana"),
  and against risk keywords such as *hack, exploit, delisting, halted, lawsuit, SEC charges, depeg*. If **all** feeds are down,
  the check is skipped and a skip is logged.
- **Data problems never crash the cycle.** A failed depth fetch or a missing feed is logged in `skips.jsonl` and reported in
  `status.json` → `data_health`.

## Event calendar

`events.json` is re-read every cycle, so edits apply without a restart.

```json
{
  "events": [
    {"type": "macro",  "name": "FOMC rate decision", "time_utc": "2027-01-27T19:00:00Z", "symbols": ["*"]},
    {"type": "unlock", "name": "Example token unlock", "time_utc": "2027-02-01T00:00:00Z", "symbols": ["ABC"],
     "block_before_hours": 24, "block_after_hours": 24}
  ]
}
```

- `macro` events block **all** symbols; `unlock` events block the listed **base** tickers (for example `"SOL"`, not `"SOLUSDT"`).
- The blocking window is `[time_utc - block_before_hours, time_utc + block_after_hours]`. Defaults: `event_horizon_hours` (24) before, 0 after.
- The bundled `events.json` is an **example snapshot** and goes out of date. Maintain it from official sources
  (central-bank calendars, statistics offices, project announcements and token-unlock trackers).

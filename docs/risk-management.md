# Risk management

> [!CAUTION]
> These controls reduce some risks. They do **not** make trading safe, and they cannot prevent losses.

## Layers of protection

| Layer | What it does | Where |
|-------|--------------|-------|
| Live opt-in | No real order unless `[live] enabled = true` + `--mode live` + keys + pre-flight + `YES`. | `main.py`, `desk/preflight.py` |
| Key hygiene | Pre-flight refuses withdrawal-enabled keys by default and warns when there is no IP restriction. | `desk/preflight.py` |
| Allowlist | Orders for symbols outside `allowed_symbols` are refused before they are sent. | `desk/live.py` |
| Filters | Spread, funding, volume, depth, news and event checks. | `desk/filter.py` |
| Score threshold | Only candidates scoring at least `min_score`. | `desk/analyst.py` |
| Sizing limits | One entry per cycle, `max_open_positions`, `max_position_fraction`, cooldown. | `desk/executor.py` |
| Stop before entry | No buy unless a valid protective stop can be placed. | `desk/executor.py`, `desk/risk.py` |
| Exchange-held stop | The stop order rests on Binance, so it works while the desk is offline. | `desk/live.py` |
| Trailing stop | Locks in part of a gain; it only moves up. | `desk/risk.py` |
| Emergency exit | Market sell if a stop cannot be placed or replaced, or the price gapped below the stop limit. | `desk/live.py` |
| Reconciliation | On restart, state is checked against real balances and orders. | `desk/live.py` |
| Target stop | Stops trading when `target_equity` is reached. | `main.py` |
| Manual controls | Stop, pause, resume, close now. | `deskctl.py`, dashboard |

## Known risks and limits

- **Gaps and slippage.** A `STOP_LOSS_LIMIT` order may not fill if the price falls through the limit. The desk then sells at market,
  but only while it is running. With `stop_mode = "oco"` the same applies to the stop leg.
- **Small-balance concentration.** Below `small_balance_threshold` (50 USDT) the desk uses almost the whole balance for a single position.
- **Fees and dust.** Binance takes fees in the bought coin, and lot-size rounding can leave small unsellable remainders ("dust").
- **Data quality.** Feeds can be late, wrong or unavailable. When a non-critical feed is down, its check is skipped and logged.
- **Event calendar.** `events.json` is maintained by hand. Missing events are not blocked.
- **News matching** is keyword-based and will miss things and produce false positives.
- **Exchange and network risk.** Outages, maintenance, rate limits, API changes or regional restrictions can stop the desk or block orders.
- **Software bugs.** Despite the tests, the code may contain mistakes. Review it before trusting it with money.
- **Strategy risk.** Momentum strategies can perform poorly in choppy or falling markets.

## Recommendations

- Start in paper mode; then go live with a small amount you can afford to lose.
- Use a dedicated API key with withdrawals off, an IP restriction and, ideally, a symbol whitelist that matches `allowed_symbols`.
- Keep `max_open_positions` and `max_position_fraction` conservative.
- Check the dashboard and `logs/` regularly. Do not leave the desk unattended for long periods.
- Keep your machine's clock synced, and keep it on a stable connection.

# Configuration reference

Settings live in a TOML file. The desk reads, in this order:

1. the file given with `--config PATH`;
2. otherwise `config.toml` in the repository root (yours, git-ignored);
3. otherwise `config.example.toml` (shipped, safe defaults).

Start with `cp config.example.toml config.toml` (Windows: `copy config.example.toml config.toml`). `--mode paper|live` on the
command line always overrides `mode`. Percent values are written as percent (`8.0` means 8%) unless the comment says otherwise.

API keys are **never** stored in the TOML file. See [Secrets](#secrets).

## Top level

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mode` | string | `"paper"` | `"paper"` or `"live"`. |
| `start_balance` | float | `1000.0` | Paper starting cash (USDT). In live mode the real account equity is used. |
| `target_equity` | float | `2000.0` | When equity reaches this, the desk stops trading, writes `state/TARGET_REACHED` and exits. Must be greater than 0. |

## `[loop]`

| Key | Default | Description |
|-----|---------|-------------|
| `scan_interval_sec` | `300` | Seconds between scan cycles (Watcher → Executor). |
| `monitor_interval_sec` | `30` | Seconds between ticks (risk checks, heartbeat, control files). |

## `[data]`

| Key | Default | Description |
|-----|---------|-------------|
| `binance_base` | `"https://data-api.binance.vision"` | Public Spot market-data host used in paper mode. Live mode uses `[live] base_url`. |
| `okx_base` | `"https://www.okx.com"` | Funding rates, primary source. |
| `gate_base` | `"https://api.gateio.ws"` | Funding rates, fallback. |
| `timeout_sec` | `12` | HTTP timeout. |
| `workers` | `8` | Parallel requests per scan. Keep it modest to respect rate limits. |

## `[watcher]`

See [agents/watcher.md](agents/watcher.md). Keys: `allowed_symbols`, `majors`, `top_alts`, `min_alt_quote_volume`, `exclude_regex`, `exclude_bases`.

## `[filter]`

See [agents/filter.md](agents/filter.md).

| Key | Default | Description |
|-----|---------|-------------|
| `max_spread_pct` | `0.10` | Maximum bid/ask spread, percent of mid. |
| `max_abs_funding_8h` | `0.0005` | Maximum absolute funding rate per 8h, as a **fraction** (0.0005 = 0.05%). |
| `min_volume_ratio` | `1.0` | Minimum 24h volume / 10-day average. |
| `depth_pct` | `1.0` | Order-book window around mid, percent. |
| `min_depth_usd` | `100_000` | Minimum notional on the thinner side within the window. |
| `funding_missing_policy` | `"pass"` | `"pass"` or `"kill"` when no source lists the coin. |
| `event_horizon_hours` | `24` | Default blocking time before an event. |
| `events_file` | `"events.json"` | Event calendar, relative to the repository root. |
| `news_lookback_hours` | `12` | Age limit for headlines. |
| `news_feeds` | 4 RSS feeds | RSS or Atom URLs. |
| `news_risk_keywords` | list | Words that mark a headline as risky. |

## `[analyst]`

See [agents/analyst.md](agents/analyst.md). Keys: `min_score` (0.6), `w_momentum` (0.40), `w_trend` (0.35), `w_volume` (0.25).

## `[executor]`

See [agents/executor.md](agents/executor.md). Keys: `fee_rate`, `small_balance_threshold`, `small_balance_fraction`,
`max_position_fraction`, `max_open_positions`, `min_notional_usd`, `reentry_cooldown_min`.

## `[risk]`

See [agents/risk.md](agents/risk.md). Keys: `stop_loss_pct` (8.0), `trail_activate_pct` (10.0), `trail_pct` (5.0).

## `[live]`

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | **Master switch.** Must be exactly `true` for `--mode live` to run. |
| `base_url` | `"https://api.binance.com"` | Binance Spot REST endpoint for orders, account and prices. |
| `futures_base` | `"https://fapi.binance.com"` | Binance Futures, used only to read funding rates. Leave empty to disable. |
| `quote_asset` | `"USDT"` | Quote currency. |
| `min_start_usdt` | `6.0` | Pre-flight refuses to start with less free quote balance (unless a desk position is open). |
| `small_balance_fraction` | `0.98` | Share of free balance used in small-balance mode. |
| `stop_mode` | `"stop_limit"` | `"stop_limit"` or `"oco"`. |
| `stop_limit_buffer_pct` | `1.0` | Limit price below the stop trigger, percent. |
| `oco_take_profit_pct` | `300.0` | OCO only: take-profit distance above entry, percent. |
| `trail_min_step_pct` | `0.25` | Minimum improvement before replacing the stop, percent. |
| `recv_window_ms` | `5000` | Binance `recvWindow` for signed requests. |
| `allow_withdrawal_enabled_key` | `false` | Pre-flight refuses keys with withdrawals enabled unless this is `true`. Not recommended. |

## Secrets

Live mode reads `BINANCE_API_KEY` and `BINANCE_API_SECRET` from, in order of priority:

1. real environment variables;
2. `keys.txt` in the repository root (a convenience for Windows users; same `NAME=value` format);
3. `.env` in the repository root (copy [`.env.example`](../.env.example)).

All three files are git-ignored. Keys are validated (letters and digits only) and are never printed or logged.

```bash
# macOS / Linux, current shell only
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
```

```powershell
# Windows PowerShell, current window only
$env:BINANCE_API_KEY = "..."
$env:BINANCE_API_SECRET = "..."
```

## Tuning tips

- Change one thing at a time and compare in paper mode over many cycles. `kills.jsonl` shows which check rejects most candidates.
- Stricter filters mean fewer trades, not necessarily better ones.
- Do not lower `stop_loss_pct` below what normal volatility requires, or stops will trigger on noise. Do not remove it.

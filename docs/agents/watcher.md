# Agent 1: Watcher

**Code:** [`desk/watcher.py`](../../desk/watcher.py) · **Config:** `[watcher]`

The Watcher decides **which coins are looked at** in a scan cycle and gathers the data the other agents need.

## Universe

Without an allowlist (`allowed_symbols = []`):

1. Start from all Binance Spot 24h tickers.
2. Keep only `...USDT` pairs.
3. Drop `exclude_bases` (stablecoins, fiat, wrapped assets), leveraged tokens (`...UP`, `...DOWN`, `...BULL`, `...BEAR`)
   and symbols matching `exclude_regex` (tokenised stocks and ETFs such as `NVDAB`), except the majors.
4. Drop stablecoin-like pairs: price between 0.97 and 1.03 with less than 1% 24h range.
5. Select the `majors` (BTC, ETH and BNB by default) plus the `top_alts` highest-volume alts with at least `min_alt_quote_volume` USDT of 24h volume.

With an allowlist, **only** the listed symbols are considered, all of them, ranked by 24h volume. `top_alts`,
`min_alt_quote_volume` and `exclude_regex` no longer apply, but the stablecoin and leveraged-token exclusions still do.
Majors are included only if they are listed. Listed symbols that are not currently trading are reported once in `desk.log`.

## Data gathered per candidate

- Last price, 24h quote volume and 24h change.
- Best bid and ask with sizes (book ticker).
- Average daily quote volume of the previous 10 **closed** daily candles.
- The last 99 **closed** hourly candles (closes and quote volumes).

Requests run in parallel with `[data] workers` threads.

## Settings

| Key | Default | Description |
|-----|---------|-------------|
| `allowed_symbols` | `[]` | Allowlist. Empty means no restriction. |
| `majors` | `["BTCUSDT","ETHUSDT","BNBUSDT"]` | Always watched when there is no allowlist. |
| `top_alts` | `20` | Number of alts selected by volume. |
| `min_alt_quote_volume` | `5_000_000` | Minimum 24h USDT volume for an alt. |
| `exclude_regex` | `"^[A-Z]{2,5}B$"` | Base assets to skip (tokenised stocks). |
| `exclude_bases` | stablecoins, fiat, wrapped | Base assets never traded. |

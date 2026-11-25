# Agent 4: Executor

**Code:** [`desk/executor.py`](../../desk/executor.py) · **Config:** `[executor]`, `[live]`

The Executor turns the best-ranked candidate into a position. It opens **at most one new position per cycle**.

## Rules (paper and live)

- Skip symbols already held, symbols in their re-entry cooldown (`reentry_cooldown_min`) and, in live mode, symbols outside the
  allowlist or blocked for the session.
- Skip symbols whose exchange status is not `TRADING`.
- **Position count:** one position when equity is below `small_balance_threshold`, otherwise up to `max_open_positions`.
- **Size:**
  - below `small_balance_threshold`: about the whole free balance (`small_balance_fraction`: 0.99 paper, `[live] small_balance_fraction` 0.98 live);
  - otherwise: `min(free cash, equity * max_position_fraction)`.
- Quantity is rounded **down** to the lot step. If the result is below the minimum quantity or minimum notional, the next candidate is tried.
- In small-balance mode, if the lot step would waste more than 15% (paper) or 10% (live) of the budget, the next candidate is tried.
- The Risk agent must **approve** the order and set its initial stop **before** the buy.

## Live specifics

- `plan_live` is a pure check: the buy is only sent if, after Binance's fee is deducted in the coin, the remaining quantity can carry a
  valid protective stop above Binance's minimums, even at the lowest stop price that could ever be used.
- An unresolved earlier order blocks new buys until reconciliation has confirmed what happened.
- A clear rejection tries the next candidate. An unclear outcome (for example a timeout) stops buying for this cycle, and the next tick reconciles.
- A −2010 "symbol not whitelisted" error is logged as a kill, blocks that symbol for the session and tries the next candidate.

## Paper fills

Buys fill at the current **ask** and sells at the **bid** (or the stop level, see [risk.md](risk.md)), and both pay `fee_rate` (0.1%).

| Key | Default | Description |
|-----|---------|-------------|
| `fee_rate` | `0.001` | Fee per side (paper simulation and live sizing). |
| `small_balance_threshold` | `50.0` | Below this equity: single, nearly full-balance position. |
| `small_balance_fraction` | `0.99` | Paper share of cash used in small-balance mode. |
| `max_position_fraction` | `0.10` | Maximum share of equity per trade. |
| `max_open_positions` | `3` | Maximum simultaneous positions. |
| `min_notional_usd` | `5.0` | Lower bound for the order value (exchange filters may require more). |
| `reentry_cooldown_min` | `60` | Minutes before re-entering a symbol after an exit. |

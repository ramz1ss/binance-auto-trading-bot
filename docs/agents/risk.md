# Agent 5: Risk

**Code:** [`desk/risk.py`](../../desk/risk.py), live order handling in [`desk/live.py`](../../desk/live.py) · **Config:** `[risk]`, `[live]`

## Initial stop

`approve(order)` sets `stop = entry reference price * (1 - stop_loss_pct / 100)` **before** the order is sent. After a live fill,
the stop is recomputed from the real average fill price.

## Trailing stop

- Track the **peak** price since entry (from 1-minute candle highs and the current bid).
- When `peak ≥ entry * (1 + trail_activate_pct / 100)`, trailing is active.
- Trailing stop = `peak * (1 - trail_pct / 100)`. The stop **only ever moves up**.

With the defaults (8 / 10 / 5) a trade risks about −8% plus fees at first. Once it has gained 10%, the stop rises to at least +4.5% above entry
(`1.10 * 0.95 = 1.045`).

## Paper monitoring

Every tick, the 1-minute candles since the last check are replayed **conservatively**: within a candle the **low is assumed to happen before the high**.
If the low touches the stop, the position exits at the stop price, or at the candle's open if the market gapped through it.
The current bid is checked as well. Exits are labelled `stop_loss` or `trailing_stop`.

## Live monitoring

The real stop order sits on Binance. Every tick the desk:

1. Checks whether the stop has filled (fully or partly) and records the exit.
2. Updates the peak and asks Risk whether the stop should move (`live_update`).
3. Replaces the stop when the new level is at least `trail_min_step_pct` higher (cancel, then place), to save API calls.
4. Sells at market (**emergency exit**) if a stop cannot be placed or replaced, or the price has fallen below the stop's limit price
   so the resting order could not fill.

| Key | Default | Description |
|-----|---------|-------------|
| `[risk] stop_loss_pct` | `8.0` | Initial stop distance, percent. |
| `[risk] trail_activate_pct` | `10.0` | Gain that activates trailing, percent. |
| `[risk] trail_pct` | `5.0` | Trailing distance below the peak, percent. |
| `[live] stop_mode` | `"stop_limit"` | `stop_limit` (`STOP_LOSS_LIMIT` sell) or `oco`. |
| `[live] stop_limit_buffer_pct` | `1.0` | Limit price sits this far below the stop price so it fills in a fast drop. |
| `[live] oco_take_profit_pct` | `300.0` | Only for `oco`: take-profit leg placed far above entry. |
| `[live] trail_min_step_pct` | `0.25` | Minimum improvement before the stop order is replaced. |

See also [../risk-management.md](../risk-management.md).

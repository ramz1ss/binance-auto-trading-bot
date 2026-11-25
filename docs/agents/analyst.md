# Agent 3: Analyst

**Code:** [`desk/analyst.py`](../../desk/analyst.py) · **Config:** `[analyst]`

The Analyst gives each Filter survivor a score from 0 to 1 and rejects those below `min_score`. Survivors are ranked by score.
Candidates with fewer than 60 hourly candles score 0.

```
score = w_momentum * momentum + w_trend * trend + w_volume * volume
```

| Factor | Computation (hourly closes) |
|--------|-----------------------------|
| **Momentum** | `0.5 * clip(0.5 + r6 / 0.08) + 0.5 * clip(0.5 + r24 / 0.20)`, where `r6` and `r24` are the 6h and 24h returns. Multiplied by 0.8 when RSI(14) > 75 (overbought). |
| **Trend** | `0.35 * [close > EMA20] + 0.35 * [EMA20 > EMA50] + 0.30 * clip(0.5 + EMA50 12h slope / 0.02)` |
| **Volume** | `0.5 * clip(24h volume / 10d average / 2) + 0.5 * clip(avg of last 6h / avg of last 24h / 2)` |

`clip` limits a value to the range 0 to 1. Every factor and its inputs are logged in `cycles.jsonl` (ranked candidates) or in `kills.jsonl`.

| Key | Default |
|-----|---------|
| `min_score` | `0.6` |
| `w_momentum` | `0.40` |
| `w_trend` | `0.35` |
| `w_volume` | `0.25` |

The weights should add up to 1.0 so that scores stay between 0 and 1.

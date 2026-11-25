# The six agents

The desk is a pipeline of six small components. Each one does one job and logs what it decided.

| # | Agent | Input | Output | Doc |
|---|-------|-------|--------|-----|
| 1 | Watcher | Exchange tickers | Candidates with prices, books and candles | [watcher.md](watcher.md) |
| 2 | Filter | Candidates | Survivors (rejections are logged) | [filter.md](filter.md) |
| 3 | Analyst | Survivors | Ranked candidates with a score | [analyst.md](analyst.md) |
| 4 | Executor | Ranked candidates | At most one new position per cycle | [executor.md](executor.md) |
| 5 | Risk | Orders and open positions | Initial stop, trailing stop, exits | [risk.md](risk.md) |
| 6 | Logger | Everything | Log files and `status.json` | [logger.md](logger.md) |

"Agent" here means a deterministic, rule-based component. There is no machine-learning model and no external AI service.

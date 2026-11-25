# Troubleshooting

Start with `logs/desk.log` (`paper/logs/desk.log` for paper) and the last lines printed in the console.
`status.json` → `data_health` shows the state of each data source.

| Symptom | Cause and fix |
|---------|---------------|
| `Python 3.11 or newer was not found` | Install Python 3.11+ and tick **Add Python to PATH** (Windows). On macOS and Linux, run `python3 -V`. |
| `ModuleNotFoundError: tomllib` | Your Python is older than 3.11. |
| `[STOP] Live trading is disabled` | Expected with the shipped config. Set `[live] enabled = true` in `config.toml` (see [paper-vs-live.md](paper-vs-live.md)). |
| `Your Binance key and secret are not filled in yet` | Set `BINANCE_API_KEY`/`BINANCE_API_SECRET`, or fill in `.env`. Placeholder values are rejected. |
| `does not look right` (key) | Copy the key again: no spaces, quotes or line breaks. |
| `Binance rejected the API key or secret` | Wrong key or secret, Reading not enabled, or the IP restriction does not include this machine's public IP. |
| `WITHDRAWALS ARE ENABLED on this API key` | Untick withdrawals on the key (recommended), or set `[live] allow_withdrawal_enabled_key = true`. |
| `HTTP 451` / `refuses connections from this location` | Binance is not available from your location. Paper mode still works through `data-api.binance.vision`. |
| `clock is too far off` / error −1021 | Sync your system clock (Windows: Settings → Time → *Sync now*; Linux: enable NTP). |
| `Only X USDT is free` | Move USDT to the Spot wallet; at least `min_start_usdt` is needed. |
| `The desk is already running` | Another instance holds `state/desk.lock`. Close it or run STOP. If none is running, delete `state/desk.lock` and `state/desk.pid`. |
| `A STOP request is still pending` | Delete `state/STOP`, or start with `START.bat`, which clears it. |
| Desk exits immediately with `TARGET REACHED` | `target_equity` is reached. Raise it in `config.toml` and delete `state/TARGET_REACHED`. |
| No trades for a long time | Normal in quiet or risky markets. Check `kills.jsonl`: most often spread, depth, volume ratio or a score below `min_score`. |
| `cannot size ... below Binance minimum` | The balance is too small for the order plus its protective stop. Add funds, or the desk keeps skipping. |
| `not whitelisted` (−2010) | The key's symbol whitelist does not include that symbol. Add it on Binance and to `allowed_symbols`, or ignore it: it is blocked for the session. |
| `all news feeds unavailable` | Feeds are down or blocked; the news check is skipped and logged. Replace URLs in `news_feeds` if needed. |
| Dashboard says `NO HEARTBEAT` | The desk is not running, or `status.json` is stale (older than about 3 minutes). |
| Dashboard port in use | Start with `--port 8766`. |
| Phone cannot open the dashboard | Use `VISUALIZER-PHONE.bat` or `--host 0.0.0.0`, the same Wi-Fi, and allow Python through the firewall. |
| Many `tick error` lines, then exit | 20 consecutive errors make the desk exit so the supervisor restarts it with a fresh pre-flight. Read the traceback in `desk.log`. |

## Getting help

Open an issue using the bug-report template. **Never post API keys, `.env`, `keys.txt`, account IDs, balances or unredacted logs.**
Security problems: see [SECURITY.md](../SECURITY.md).

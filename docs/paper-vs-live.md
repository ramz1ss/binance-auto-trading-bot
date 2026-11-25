# Paper vs live

> [!WARNING]
> Live mode trades **real money**. You can lose some or all of it. Read [risk-management.md](risk-management.md) and the
> [disclaimer](../README.md#disclaimer) first. Nothing here is financial advice.

## Paper mode (default)

- Simulated cash (`start_balance`), **real public prices** from Binance's market-data API.
- No account, no keys, nothing is bought.
- Same agents and same risk rules as live; fills are simulated at bid/ask with fees, and stops are checked on 1-minute candles.
- Files go to `paper/`, separate from live files.

```bash
python main.py --mode paper          # or windows\PAPER.bat, or scripts/paper.sh
python main.py --mode paper --once   # a single scan cycle
```

Reset the paper account by stopping the desk and deleting the `paper/` folder.

Paper results are **optimistic**: they do not model slippage, partial fills, queue position, API outages or exchange-side
stop behaviour exactly. Treat them as a functional test, not as an expectation of returns.

## Going live: checklist

Do these in order. Stop at any step you are not comfortable with.

### 1. Run paper mode first

Run it long enough to see many cycles, a few trades and how the filters behave. Use the dashboard and `logs/`.

### 2. Create a dedicated Binance API key

In Binance → **API Management**, create a new key used only by Binance Auto Trading Bot (Crypto Desk):

| Setting | Value |
|---------|-------|
| Enable Reading | **On** |
| Enable Spot & Margin Trading | **On** (the desk only uses Spot) |
| Enable Withdrawals | **Off**. The pre-flight check refuses keys with withdrawals on. |
| Enable Futures / Margin / Universal Transfer | **Off** |
| IP access restriction | **Restrict to trusted IPs**: your machine's public IP |
| Symbol whitelist (optional) | The symbols you want to trade, copied into `allowed_symbols` |

Fund the Spot wallet only with an amount you can afford to lose.

### 3. Provide the keys

Use environment variables (recommended), or copy `.env.example` to `.env` and fill it in:

```dotenv
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

`.env` and `keys.txt` are git-ignored. Never commit, paste or screenshot them. If a key is ever exposed, **delete it on Binance immediately**.

### 4. Opt in

In your `config.toml`:

```toml
[live]
enabled = true
```

Consider setting a small `allowed_symbols` list and keeping the conservative executor defaults.

### 5. Run the pre-flight check

```bash
python main.py --mode live --preflight-only   # or windows\CHECK-KEYS.bat
```

It checks, without trading:

1. keys are present and well-formed;
2. `api.binance.com` is reachable from your location (HTTP 451 means Binance is not available there);
3. your clock is in sync with Binance;
4. the key and secret are valid, and the account can trade Spot;
5. the key has Spot trading on, **withdrawals off** (or you explicitly accepted the risk), and an IP restriction (a warning if missing);
6. there is at least `min_start_usdt` free; coins the desk did not buy are listed and ignored;
7. the Binance funding-rate feed is reachable (optional; OKX/Gate are used otherwise).

### 6. Start

```bash
python main.py --mode live    # or windows\START.bat, or scripts/live.sh
```

The first time, you must type `YES`. After that, the confirmation is remembered in `state/live_confirmed`. The supervisors
(`START.bat`, `scripts/live.sh`, systemd) restart the desk after crashes and temporary problems, but not after exit code 2.

## Turning live off again

- **Stop the program:** `STOP.bat` / `scripts/stop.sh` / Ctrl+C. Open positions **keep their stop-loss on Binance**.
- **Stop new entries but keep managing trades:** `PAUSE.bat` or `python deskctl.py pause --mode live`.
- **Sell now:** `CLOSE-TRADE.bat` or `python deskctl.py close --mode live` (asks for `YES`).
- **Disable live completely:** set `[live] enabled = false` and delete or disable the API key on Binance.

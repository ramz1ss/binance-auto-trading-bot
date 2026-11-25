# Deployment

Binance Auto Trading Bot (Crypto Desk) runs from a folder: there is nothing to install system-wide. All you need is Python 3.11+ and the pinned requirements.

## Windows (desktop)

1. Install Python 3.11+ from [python.org](https://www.python.org/downloads/) with **Add Python to PATH**.
2. Put the repository in a folder you control, for example `C:\CryptoDesk`.
3. Double-click `windows\PAPER.bat` (paper) or, after opting in, `windows\START.bat` (live).
   The first run creates `venv\` and installs `requirements.txt`. Later runs start immediately.
4. Optional: `windows\CREATE-SHORTCUT.bat` adds a "Binance Auto Trading Bot" dashboard shortcut to the Desktop and Start menu.

`START.bat` is a supervisor: it restarts the desk after a crash (10 s) or a temporary problem (60 s), and stops for exit code 0 or 2.
Keep the window open. Disable sleep in the Windows power settings if the desk should run unattended.

## macOS and Linux (desktop)

```bash
scripts/setup.sh        # venv + requirements + config.toml
scripts/paper.sh        # paper
scripts/live.sh         # live supervisor (after opting in)
scripts/stop.sh         # clean stop
scripts/visualizer.sh   # dashboard
```

Use `tmux` or `screen` to keep the desk running after you close the terminal.

## Linux server (systemd)

A hardened unit is provided in [`deploy/crypto-desk.service`](../deploy/crypto-desk.service).

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin cryptodesk
sudo install -d -o cryptodesk -g cryptodesk /opt/crypto-desk
sudo -u cryptodesk git clone https://github.com/ramz1ss/binance-auto-trading-bot.git /opt/crypto-desk
cd /opt/crypto-desk && sudo -u cryptodesk scripts/setup.sh

# keys, readable by root only (systemd loads them for the service)
sudo install -m 600 -o root -g root /dev/null /etc/crypto-desk.env
sudoedit /etc/crypto-desk.env   # two lines: BINANCE_API_KEY=...  and  BINANCE_API_SECRET=...

sudo -u cryptodesk nano /opt/crypto-desk/config.toml   # [live] enabled = true, allowlist, limits

# one-off pre-flight check as the service user (keys come from the environment, never from the command line)
sudo bash -c 'set -a; . /etc/crypto-desk.env; set +a; exec runuser -u cryptodesk -- \
  /opt/crypto-desk/venv/bin/python /opt/crypto-desk/main.py --mode live --preflight-only'

sudo cp deploy/crypto-desk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-desk
journalctl -u crypto-desk -f
```

The unit runs `main.py --mode live --yes` (`--yes` replaces the interactive confirmation), restarts on failure, and does **not**
restart on exit code 2 (a problem that needs you). Stop it with `sudo systemctl stop crypto-desk`. Open positions keep their stops on Binance.

To view the dashboard on a server, keep it on loopback and use an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 you@server   # then, on the server: venv/bin/python visualizer.py --mode live
# open http://127.0.0.1:8765 on your computer
```

Do **not** expose the dashboard to the internet.

## Choosing where to run

- Binance must be available from the machine's IP address and your jurisdiction, and the key's IP restriction must match it.
- Keep the system clock synced (NTP); signed requests fail when the clock is off.
- A stable connection matters more than raw speed: the desk trades on a minutes timescale.

## Updating

```bash
scripts/stop.sh             # or STOP.bat; open live positions stay protected
git pull
venv/bin/python -m pip install -r requirements.txt   # Windows: delete venv\.deps_ok, then start again
python -m pytest -q          # optional, needs requirements-dev.txt
```

Read [CHANGELOG.md](../CHANGELOG.md) before updating, and compare your `config.toml` with the new `config.example.toml`.

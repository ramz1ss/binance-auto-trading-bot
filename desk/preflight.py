"""Start-up safety check for LIVE mode. Refuses to trade (returns ok=False) if any step fails, with a plain-English reason.
kind: "permanent" = needs the user to fix something (do not auto-restart), "transient" = network problem (auto-restart may retry)."""

from .binance import BinanceClient, BinanceError, NetworkError
from .keys import load_keys
from .util import redact


class Result:
    def __init__(self):
        self.ok = True
        self.kind = None
        self.lines = []
        self.client = None
        self.usdt = None

    def add(self, tag, text):
        self.lines.append(f"[{tag}] {text}")

    def fail(self, text, kind="permanent"):
        self.add("STOP", text)
        self.ok = False
        self.kind = self.kind or kind
        return self


def run(cfg, client=None, say=print):
    """Runs all checks in order. `client` can be injected (tests). Prints every line as it happens (say) and returns Result."""
    r = Result()
    L = cfg["live"]

    def out(tag, text):
        r.add(tag, text)
        say(r.lines[-1])

    def stop(text, kind="permanent"):
        r.fail(text, kind)
        say(r.lines[-1])
        return r

    say("=== Pre-flight check (nothing is traded until every step passes) ===")

    # 1) keys
    if client is None:
        key, sec, problem = load_keys(cfg["_root"])
        if problem:
            return stop(problem)
        client = BinanceClient(key, sec, base=L["base_url"], recv_window=L.get("recv_window_ms", 5000))
    r.client = client
    out("OK", "Found your API key and secret (environment, .env or keys.txt; they are never shown or logged).")

    # 2) reach Binance
    try:
        client.ping()
        offs = client.sync_time()
    except BinanceError as e:
        if e.blocked_location:
            return stop(
                "Binance refuses connections from this location (HTTP 451). This PC's internet connection is in a place Binance does not serve, or a VPN is on. Turn off any VPN or run it from a supported country."
            )
        return stop(f"Binance answered with an error when we said hello: {redact(e)}. Wait a minute and try again.", "transient")
    except NetworkError as e:
        return stop(
            f"Cannot reach api.binance.com. Check that this PC is online and that a firewall/antivirus is not blocking Python. ({redact(e)})",
            "transient",
        )
    out("OK", "Reached api.binance.com.")
    if abs(offs) > 1000:
        out(
            "WARN",
            f"Your PC clock is {abs(offs) / 1000:.1f} s {'behind' if offs > 0 else 'ahead of'} Binance's clock. The desk corrects for this automatically, but please turn on Windows time sync (Settings > Time & language > Date & time > Sync now).",
        )
    else:
        out("OK", "PC clock matches Binance's clock.")

    # 3) keys valid + can trade
    try:
        acc = client.account()
    except BinanceError as e:
        if e.code in (-2014, -2015, -1022):
            return stop(
                "Binance rejected the API key or secret (Invalid API-key, IP, or permissions). Check you copied both correctly, that Reading is enabled, and - if you set an IP restriction - that it contains THIS PC's public IP address."
            )
        if e.code == -1021:
            return stop("Your PC clock is too far off for Binance. Sync the Windows clock and try again.", "transient")
        if e.blocked_location:
            return stop("Binance refuses connections from this location (HTTP 451).")
        return stop(f"Binance would not show the account: {redact(e)}", "transient" if e.maybe_executed else "permanent")
    except NetworkError as e:
        return stop(f"Lost the connection while checking the account. ({redact(e)})", "transient")
    out("OK", "API key and secret are valid.")
    if not acc.get("canTrade"):
        return stop("This Binance account says it cannot trade (canTrade is false). Complete Binance verification or contact Binance support.")
    if acc.get("accountType", "SPOT") != "SPOT" and "SPOT" not in acc.get("permissions", ["SPOT"]):
        return stop("This account does not have Spot trading.")
    out("OK", "Account can trade Spot (canTrade = true).")

    # 4) key permissions: spot trading on, withdrawals off
    try:
        pr = client.api_restrictions()
        if not pr.get("enableSpotAndMarginTrading", True):
            return stop(
                "This API key does NOT have 'Enable Spot & Margin Trading' switched on. Edit the key on Binance, tick it, save, and try again."
            )
        out("OK", "API key has Spot trading enabled.")
        if pr.get("enableWithdrawals") and L.get("allow_withdrawal_enabled_key") is not True:
            return stop(
                "WITHDRAWALS ARE ENABLED on this API key. The desk never withdraws, but a leaked key with withdrawals could empty your account. "
                "Edit the key on Binance and untick 'Enable Withdrawals' (recommended), or set [live] allow_withdrawal_enabled_key = true to accept the risk."
            )
        if pr.get("enableWithdrawals"):
            out(
                "WARN",
                "WITHDRAWALS ARE ENABLED on this API key. The desk never withdraws, but if the key ever leaks, someone could empty your account. Strongly recommended: edit the key on Binance and untick 'Enable Withdrawals'.",
            )
        else:
            out("OK", "Withdrawals are disabled on this key (good).")
        if not pr.get("ipRestrict"):
            out("WARN", "This API key has no IP restriction. It is safer to restrict it to this PC's public IP address in the Binance API settings.")
        else:
            out("OK", "API key is restricted to trusted IP addresses (good).")
    except BinanceError as e:
        out(
            "WARN",
            f"Could not read the key's permission settings ({redact(e)}), so I cannot confirm withdrawals are off. Please check on Binance yourself.",
        )
    except NetworkError as e:
        out("WARN", f"Could not read the key's permission settings ({redact(e)}).")

    # 5) balance
    q = L.get("quote_asset", "USDT")
    bal = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in acc.get("balances", [])}
    free = {b["asset"]: float(b["free"]) for b in acc.get("balances", [])}
    r.usdt = free.get(q, 0.0)
    out("OK", f"{q} balance available to trade: {r.usdt:,.4f}")
    mine = _desk_bases(cfg)
    others = {a: v for a, v in bal.items() if a != q and v > 0 and a not in mine}
    if others:
        out(
            "INFO",
            "Other coins on this account ("
            + ", ".join(sorted(others)[:8])
            + (", ..." if len(others) > 8 else "")
            + ") are NOT touched by the desk and are not counted toward the target.",
        )
    need = L.get("min_start_usdt", 6.0)
    if r.usdt < need and not _holds_desk_position(cfg):
        return stop(
            f"Only {r.usdt:.2f} {q} is free in your Spot wallet. The desk needs at least {need:.2f} {q} (Binance's minimum order is about 5, and a protective stop must fit too). Move {q} into the Spot wallet and try again."
        )

    # 6) optional: futures funding feed
    if cfg["data"].get("binance_futures_base"):
        try:
            import requests

            rr = requests.get(cfg["data"]["binance_futures_base"] + "/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"}, timeout=8)
            out(
                "OK" if rr.status_code == 200 else "WARN",
                "Binance funding-rate feed reachable."
                if rr.status_code == 200
                else f"Binance funding-rate feed answered HTTP {rr.status_code}; the desk will use OKX/Gate funding data instead.",
            )
        except Exception:
            out("WARN", "Binance funding-rate feed not reachable; the desk will use OKX/Gate funding data instead.")
    say("=== Pre-flight passed. Starting the desk. ===" if r.ok else "=== Pre-flight FAILED. Nothing was traded. ===")
    return r


def _desk_positions(cfg):
    import json
    import os

    p = os.path.join(cfg["_data"], "state", "live_state.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("positions") or {}
    except Exception:
        return {}


def _holds_desk_position(cfg):
    return bool(_desk_positions(cfg))


def _desk_bases(cfg):
    return {p.get("base") for p in _desk_positions(cfg).values()}

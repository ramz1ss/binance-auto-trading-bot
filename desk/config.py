import os
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def allowed_symbols(cfg):
    """The symbol allowlist as an upper-case set, or None when there is no restriction (empty / missing list)."""
    raw = (cfg.get("watcher") or {}).get("allowed_symbols") or []
    out = {str(s).strip().upper() for s in raw if str(s).strip()}
    return out or None


def default_path():
    """config.toml (your own, git-ignored) if it exists, else the shipped config.example.toml (safe paper defaults)."""
    own = os.path.join(ROOT, "config.toml")
    return own if os.path.exists(own) else os.path.join(ROOT, "config.example.toml")


def live_enabled(cfg):
    """Live trading is OFF unless [live] enabled = true is set explicitly in the config file."""
    return (cfg.get("live") or {}).get("enabled") is True


def load(path=None, mode=None):
    with open(path or default_path(), "rb") as f:
        cfg = tomllib.load(f)
    cfg["_root"] = ROOT
    if mode:
        cfg["mode"] = mode
    if cfg["mode"] not in ("paper", "live"):
        raise SystemExit(f"unknown mode {cfg['mode']!r} (use paper or live)")
    # Live files (logs, state, status.json) live in the main folder; paper-test files live in .\paper so the two never mix.
    cfg["_data"] = ROOT if cfg["mode"] == "live" else os.path.join(ROOT, "paper")
    for d in ("state", "logs"):
        os.makedirs(os.path.join(cfg["_data"], d), exist_ok=True)
    if cfg["mode"] == "live":
        cfg["data"]["binance_base"] = cfg["live"]["base_url"]  # real Binance endpoint for prices/books/filters
        cfg["data"]["binance_futures_base"] = cfg["live"].get("futures_base", "")  # real funding rates (fapi premiumIndex)
    return cfg

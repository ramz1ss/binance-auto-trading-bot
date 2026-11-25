"""Loads BINANCE_API_KEY / BINANCE_API_SECRET from environment variables, keys.txt or .env (in that order). Never prints them."""

import os
import re

from .util import register_secret

PLACEHOLDER = re.compile(r"paste|your_|xxxx|here|<|>", re.I)


def _read(path):
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as f:  # utf-8-sig: Notepad may add an invisible byte-order mark
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip().replace("export ", "")] = v.strip().strip('"').strip("'").strip()
    except FileNotFoundError:
        pass
    return out


def load_keys(root):
    """-> (key, secret, problem). problem is a plain-English string when something is wrong, else None."""
    vals = {}
    for name in (".env", "keys.txt"):  # later files win; real environment variables win over both
        vals.update(_read(os.path.join(root, name)))
    key = os.environ.get("BINANCE_API_KEY") or vals.get("BINANCE_API_KEY", "")
    sec = os.environ.get("BINANCE_API_SECRET") or vals.get("BINANCE_API_SECRET", "")
    register_secret(key)
    register_secret(sec)
    if not key or not sec or PLACEHOLDER.search(key) or PLACEHOLDER.search(sec):
        return (
            key,
            sec,
            "Your Binance key and secret are not filled in yet. Set BINANCE_API_KEY and BINANCE_API_SECRET as environment variables, or copy .env.example to .env and paste them after the = signs, then start again.",
        )
    for label, v in (("API key", key), ("API secret", sec)):
        if not re.fullmatch(r"[A-Za-z0-9]{20,128}", v):
            return (
                key,
                sec,
                f"The {label} does not look right (it should be one long string of letters and numbers, with no spaces or quotes). Copy it again from Binance.",
            )
    return key, sec, None

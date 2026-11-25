import json
import os
import tempfile
import time
from datetime import UTC, datetime


def now_ms():
    return int(time.time() * 1000)


def iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def atomic_write_json(path, obj):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    for i in range(10):  # Windows: os.replace fails if an antivirus/editor holds the file for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.2)


def ema(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    if len(closes) <= n:
        return 50.0
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


# ---- secrets never reach logs: every key/secret that is loaded gets registered here and masked in all log lines
SECRETS = set()


def register_secret(v):
    if v and len(v) >= 8:
        SECRETS.add(v)


def redact(text):
    import re

    text = str(text)
    for v in SECRETS:
        text = text.replace(v, "***")
    return re.sub(r"(signature=)[0-9a-fA-F]{16,}", r"\1***", text)


# ---- only one desk may run at a time (a second one could double-buy)
class SingleInstance:
    def __init__(self, path):
        self.path = path
        self.f = None

    def acquire(self):
        self.f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            self.f = None
            return False
        return True

    def release(self):
        if self.f:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.f.close()
            self.f = None

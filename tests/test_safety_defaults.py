"""Safe defaults of the shipped configuration and the explicit live-trading opt-in. Fully offline."""

import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as M  # noqa: E402
from desk import config as C  # noqa: E402
from desk.keys import load_keys  # noqa: E402
from desk.util import redact, register_secret  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "config.example.toml")


class SafeDefaults(unittest.TestCase):
    def test_example_config_defaults_to_paper_with_live_disabled(self):
        cfg = C.load(EXAMPLE)
        self.assertEqual(cfg["mode"], "paper")
        self.assertIs(cfg["live"]["enabled"], False)
        self.assertFalse(C.live_enabled(cfg))

    def test_live_enabled_requires_a_literal_true(self):
        self.assertFalse(C.live_enabled({}))
        self.assertFalse(C.live_enabled({"live": {"enabled": "true"}}))
        self.assertFalse(C.live_enabled({"live": {"enabled": 1}}))
        self.assertTrue(C.live_enabled({"live": {"enabled": True}}))

    def test_conservative_risk_limits(self):
        cfg = C.load(EXAMPLE)
        self.assertLessEqual(cfg["executor"]["max_position_fraction"], 0.10)
        self.assertLessEqual(cfg["executor"]["max_open_positions"], 3)
        self.assertGreater(cfg["risk"]["stop_loss_pct"], 0)
        self.assertLessEqual(cfg["risk"]["stop_loss_pct"], 10)
        self.assertEqual(cfg["watcher"]["allowed_symbols"], [])

    def test_example_config_contains_no_credentials(self):
        text = open(EXAMPLE, encoding="utf-8").read()
        self.assertNotRegex(text, r"(?i)api_(key|secret)\s*=")
        self.assertIsNone(re.search(r"[A-Za-z0-9]{48,}", text))


class LiveOptIn(unittest.TestCase):
    def test_live_mode_refuses_when_not_enabled_and_never_reaches_the_exchange(self):
        tmp = tempfile.mkdtemp(prefix="desk-safety")
        cfg_path = os.path.join(tmp, "config.toml")
        with open(EXAMPLE, encoding="utf-8") as src, open(cfg_path, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        with mock.patch("desk.config.ROOT", tmp), mock.patch("desk.preflight.run") as pf:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = M.main(["--mode", "live", "--config", cfg_path, "--yes"])
        self.assertEqual(rc, 2)
        pf.assert_not_called()
        self.assertIn("Live trading is disabled", out.getvalue())


class Secrets(unittest.TestCase):
    def test_placeholder_keys_are_rejected(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
            f.write("BINANCE_API_KEY=your_api_key_here\nBINANCE_API_SECRET=your_api_secret_here\n")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BINANCE_API_KEY", None)
            os.environ.pop("BINANCE_API_SECRET", None)
            _, _, problem = load_keys(d)
        self.assertIsNotNone(problem)

    def test_registered_secrets_are_redacted(self):
        fake = "TESTSECRET" * 4  # obviously fake, low entropy
        register_secret(fake)
        self.assertNotIn(fake, redact(f"signature failed for {fake}"))


if __name__ == "__main__":
    unittest.main()

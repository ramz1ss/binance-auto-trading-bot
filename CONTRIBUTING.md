# Contributing to Crypto Desk

Thank you for helping. Bug reports, documentation fixes, tests and careful improvements are all welcome.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rules

1. **Safety first.** Paper mode stays the default, and `[live] enabled = false` stays the default in `config.example.toml`.
   Changes that could affect real orders (sizing, stops, order types, reconciliation) need tests and a clear `CHANGELOG.md` entry.
2. **No secrets or personal data** in code, tests, issues or pull requests: no API keys, `.env`, `keys.txt`, account IDs, balances, trade history or unredacted logs.
3. **Tests stay offline.** Use the fakes in `tests/fakes.py`. The test suite blocks network access, except loopback.
4. **No profit claims** in code, docs or examples.
5. Keep dependencies minimal. Adding a runtime dependency needs a good reason.

## Development setup

```bash
git clone https://github.com/ramz1ss/binance-trading-bot.git
cd crypto-desk
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
```

## Before you open a pull request

```bash
ruff check .            # lint
ruff format .           # format
python -m pytest -q     # tests (offline)
python main.py --mode paper --once   # optional smoke test (uses public market data)
```

CI runs lint, the test suite on Python 3.11 to 3.13, a live-mode safety check and a secret scan.

## Pull requests

- One topic per pull request; keep it small and focused.
- Describe **what** changed and **why**, and how you tested it.
- Update `README.md` / `docs/` when behaviour or configuration changes.
- Add an entry under **Unreleased** in `CHANGELOG.md`.
- Use clear commit messages (for example `filter: skip depth check when the book is empty`).

## Reporting bugs and proposing features

Use the issue templates. For anything security-related, **do not open a public issue**: follow [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).

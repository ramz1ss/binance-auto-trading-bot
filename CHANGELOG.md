# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Optional sponsorship via [Buy Me a Coffee](https://buymeacoffee.com/ramziss) and USDT on TRON (TRC20), BNB Smart Chain (BEP20) or Ethereum (ERC20). See [Support this project](README.md#support-this-project).

## [1.0.0] - 2026-11-25

First public release.

### Added

- Six-agent pipeline: Watcher, Filter, Analyst, Executor, Risk and Logger.
- Paper broker with simulated fills, fees, lot sizes and conservative 1-minute candle stop checks.
- Live Binance Spot broker: market entries, exchange-side `STOP_LOSS_LIMIT` or OCO stops, trailing-stop replacement,
  emergency market exits, restart reconciliation, symbol allowlist guard and session blocklist for non-whitelisted symbols.
- Live pre-flight check: keys, connectivity, clock, permissions, withdrawals, IP restriction and balance.
- Six safety filters: spread, funding rate, volume ratio, order-book depth, news headlines and an event calendar.
- Local dashboard (`visualizer.py`) with phone view and a protected close button.
- Manual controls: stop, pause, resume, close now (`deskctl.py`, Windows launchers).
- Windows launchers, macOS/Linux scripts and a hardened systemd unit.
- Documentation, offline test suite, GitHub Actions CI (ruff, pytest on Python 3.11 to 3.13, secret scan).

### Security

- Live trading requires an explicit opt-in: `[live] enabled = true`, `--mode live`, API keys from the environment or `.env`,
  a passing pre-flight check and a one-time typed `YES`.
- The pre-flight check refuses API keys with withdrawals enabled unless `[live] allow_withdrawal_enabled_key = true`.
- API keys are redacted from all logs and error messages.

[Unreleased]: https://github.com/ramz1ss/binance-auto-trading-bot/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ramz1ss/binance-auto-trading-bot/releases/tag/v1.0.0

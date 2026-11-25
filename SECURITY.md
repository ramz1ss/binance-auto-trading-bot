# Security policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.x (latest `main`) | Yes |
| older | No |

## Reporting a vulnerability

**Please do not report security problems in public issues, discussions or pull requests.**

Report them privately through GitHub: **Security → Report a vulnerability** on this repository
([direct link](https://github.com/ramz1ss/binance-auto-trading-bot/security/advisories/new)). Include:

- a description and the potential impact;
- steps to reproduce, or a proof of concept;
- affected files or versions.

Never include real API keys, account data or funds-related details in the report.

You can expect an acknowledgement within 7 days. Once a fix is available, it will be released and credited
(unless you prefer to remain anonymous).

## Scope

In scope, for example:

- ways the desk could place orders it should not (outside the allowlist, without a stop, in paper mode, while live is disabled);
- leaks of API keys or secrets into logs, files, error messages or the dashboard;
- the dashboard's control endpoint being reachable or abusable from another site or network without the token;
- unsafe handling of files or configuration.

Out of scope: trading losses, strategy performance, and issues in Binance, OKX, Gate or other third-party services.

## If your API key is exposed

1. **Delete the key on Binance immediately** (API Management), then create a new one.
2. Check your account's order and withdrawal history.
3. Make sure `.env` and `keys.txt` are not committed anywhere (`git log --all -- .env keys.txt`).

## Hardening checklist for users

- A dedicated API key with **withdrawals disabled**, an **IP restriction**, and only Spot trading enabled.
- Keys only in environment variables or a git-ignored `.env`, never in `config.toml` or in code.
- Dashboard on `127.0.0.1` (the default); use `--host 0.0.0.0` only on a trusted network, and never expose it to the internet.
- Keep Python and the pinned dependencies up to date.

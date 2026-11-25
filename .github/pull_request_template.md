## What does this change?

<!-- A short description and the motivation. Link the issue if there is one. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor / tooling

## Checklist

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `python -m pytest` passes (tests stay offline; the exchange is mocked)
- [ ] Safe defaults are unchanged: paper mode by default, `[live] enabled = false` in `config.example.toml`
- [ ] If this touches order placement, sizing or stops: tests cover it and `CHANGELOG.md` explains the behaviour change
- [ ] No secrets, personal data, logs or trade history are included
- [ ] Documentation is updated (`README.md` / `docs/`)

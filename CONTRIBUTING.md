# Contributing

## Development setup

See **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** for Python 3.12 venvs per app, tests, and optional `sam local` / ngrok flows.

## Branches

Use **`main`** as the integration branch; **`test`** / **`prod`** track deployed environments as your team defines them. Prefer short-lived feature branches merged via PR.

## Pull requests

- Describe **what** changed and **how to test** (commands, Slack flows, env notes).
- Ensure **CI passes** (GitHub Actions) for touched areas.
- **Do not commit** secrets (`.env*`, keys, tokens). Use examples only.
- Update **docs** when behavior, env vars, or deploy steps change.

## Code style

- **Black** and **Ruff** where configured (see app `pyproject.toml` / CI).
- Keep handlers small; prefer shared helpers over copy-paste.

## Commits

Clear, imperative subject lines (`Fix qsignups AOQ filter`). Optional Conventional Commits if the team adopts them.

## License

By contributing, you agree your contributions are under the repository **AGPL-3.0** license.

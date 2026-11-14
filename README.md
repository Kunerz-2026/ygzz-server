# Yokai: Spirits Hunt (妖怪正传)

**Unofficial, fan-made server implementation.**

> This project is not affiliated with, endorsed by, or sponsored by the game's publisher or
> rights holders. It contains **server-side source code only**. No game client, installation
> package, assets, data tables, artwork, audio, or other copyrighted material is included or
> distributed here.

## Status

Work in progress. The server implements a large subset of the game's network protocol and
server-side systems. Compatibility depends on the client build and the data files you supply.
There is no warranty; use it at your own risk.

## What is (and is not) in this repository

Included:

- `run_server.py` — launcher
- `work/` — server implementation and operations tools
- `tests/` — a small, self-contained subset of the test suite
- `config.ini.example` — configuration template
- Documentation, license, and GitHub metadata

Not included:

- Game client, client patches, or installation packages
- Game data tables, Lua scripts, artwork, audio, or other assets
- Account data, save files, logs, tokens, or signing keys

You must supply your own compatible data files and client environment. This project does not
tell you where to obtain them.

## Requirements

- Python 3.10 or newer (standard library only; no third-party packages)
- A compatible game client and data tables that you are legally allowed to use
- Windows, Linux, or macOS (the server is pure Python; the launcher is portable)

## Quick start

```bash
git clone https://github.com/Kunerz-2026/Yokai-Spirits-Hunt.git
cd Yokai-Spirits-Hunt
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: . .venv/bin/activate
pip install -r requirements.txt
cp config.ini.example config.ini     # Windows: copy config.ini.example config.ini
# Place your data tables in data/tables/ (or set YGZZ_TABLE_DIR)
python run_server.py
```

Default ports: HTTP `18080`, game `27001`/`27002`. Change them in `config.ini`. Open only the
ports you need, and do not expose the admin endpoint to the public internet.

The first run creates `work/admin_token.txt`. Treat it as a password.

## Data tables

This repository does **not** ship data tables. The server reads JSON tables from:

1. `YGZZ_TABLE_DIR`, if set; otherwise
2. `data/tables/` relative to the repository root.

An optional secondary directory can be set with `YGZZ_TABLE_DIR_ALT` for legacy table sets.
If it is not set, features that depend on it degrade gracefully.

## Configuration

`config.ini.example` documents the available options: HTTP/game ports, invite code, guest
login, advertised IP, and report contact. Copy it to `config.ini` and edit locally.
`config.ini` is git-ignored.

Useful environment variables:

- `YGZZ_TABLE_DIR` — data table directory
- `YGZZ_TABLE_DIR_ALT` — optional secondary table directory
- `YGZZ_ADVERTISE_HOST` — address sent to clients
- `YGZZ_BIND_HOST` — bind address
- `YGZZ_AUTH`, `YGZZ_MULTI_ACCOUNT`, `YGZZ_PUBLIC` — security-related switches; see `run_server.py`

> The `YGZZ_` environment-variable prefix is retained for backward compatibility with existing
deployments; it is not the project name. The project name is **Yokai: Spirits Hunt (妖怪正传)**.

## Tests

A small self-contained subset is included. It does not require game data or a device:

```bash
python tests/run_tests.py
```

The full regression suite is not part of this repository because it depends on
environment-specific fixtures and game data.

## Security

- The server is intended for private/LAN use. Do not expose it to the internet without a
  firewall, authentication, and HTTPS termination.
- Keep `work/admin_token.txt`, `config.ini`, account data, and logs private.
- Report vulnerabilities through GitHub's private vulnerability reporting; see `SECURITY.md`.

## Contributing

See `CONTRIBUTING.md`. Do not submit game assets, client files, data tables, account data,
logs, or secrets.

## License

Copyright (c) 2026 kunerz.

AGPL-3.0-or-later. See `LICENSE`. The license covers the source code in this repository only;
it does not grant any rights to the game, its assets, or its trademarks.

## Disclaimer

*Yokai: Spirits Hunt* and *妖怪正传* are trademarks of their respective owners. They are used
here only to describe compatibility. If you are a rights holder and believe this repository
infringes your rights, please contact the maintainer through GitHub; we will respond promptly.

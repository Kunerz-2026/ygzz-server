# Security Policy

## Supported versions

Only the latest commit on `main` is supported. This is a hobby project with no SLA.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** for this repository:

<https://github.com/Kunerz-2026/Yokai-Spirits-Hunt/security/advisories/new>

Do **not** open a public issue with exploit details, credentials, personal data, or game
assets. If private reporting is unavailable, open a minimal public issue asking for a private
contact channel; do not include the vulnerability details.

## Scope

This policy covers the server source code in this repository. It does not cover:

- game clients, installation packages, or game assets (not distributed here);
- third-party servers or services;
- servers you do not own or administer.

Please do not test against systems you do not own. When testing locally, use isolated copies of
your data and never attach real account data, tokens, or logs to an issue.

## Operational security

- Keep `work/admin_token.txt`, `config.ini`, `work/accounts/`, and `work/logs/` private.
- Do not expose the admin endpoint or the game ports to the public internet without a firewall
  and authentication.
- Rotate the admin token if it may have leaked.
- The server is intended for private/LAN use; treat it as untrusted code and run it in an
  isolated environment if you expose it to other people.

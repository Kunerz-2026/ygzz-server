# Public test subset

Run:

```bash
python tests/run_tests.py
```

These tests are self-contained and do not need game data tables, a game client, or a device:

| Test | What it covers |
|---|---|
| `_account_reset_test.py` | account registration, password reset codes, lockout |
| `_email_account_test.py` | email-shaped account validation and login |
| `_acct_backup_test.py` | account data snapshot / verify / restore / rollback |
| `_suite_gate_test.py` | regression-runner "fake green" guard |

The full private regression suite is not included. Many of its tests depend on a specific game
client, data table set, live server, device, or environment-specific fixtures. Publishing them
without that environment would produce misleading failures and could leak private test data.

In particular, the runtime half of the address-configuration test (`_b3_address_config_test.py`)
needs a full data table set to import the server. Its static checks are useful, but they are not
part of this public subset.

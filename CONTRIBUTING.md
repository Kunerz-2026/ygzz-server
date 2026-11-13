# Contributing

Thanks for your interest. This is a hobby project; contributions are welcome but may take time
to review.

## Ground rules

- **Do not submit game assets, client files, installation packages, data tables, Lua scripts,
  artwork, audio, account data, logs, or secrets.** Such changes will be rejected.
- Keep the repository runnable without proprietary data. Tests must not require a game client.
- Do not add code that helps distribute or monetize copyrighted game content.
- Be respectful. See `CODE_OF_CONDUCT.md`.

## Pull requests

1. Fork the repository and create a topic branch.
2. Make a focused change; avoid unrelated formatting churn.
3. Run the checks locally:

   ```bash
   python -m compileall -q run_server.py work
   python tests/run_tests.py
   ```

4. Describe what changed, why, and how you tested it.
5. Confirm that your contribution contains no game assets, data, credentials, or personal data.

## Coding style

- Python 3.10+; standard library only unless discussed first.
- Keep the existing module layout. New server modules go in `work/`.
- Prefer small, testable functions and explicit error handling.
- Do not log passwords, tokens, tickets, account names, or raw request bodies containing them.

## License

By submitting a contribution, you agree that it is licensed under the repository's
AGPL-3.0-or-later license and that you have the right to submit it.

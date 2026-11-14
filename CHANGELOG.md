# Changelog

## v0.1.2 — 2026-11-14

### Fixed: players could get stuck on the loading screen at 40%

Account saves are loaded by `accounts._load()` with a plain `json.load`, which **does not** run the
missing-key defaulting that `state.load_state()` performs for the template save. A save missing one
top-level key therefore breaks the corresponding bag handler: with `equip_id` absent,
`equip_bag_json()` raised `KeyError`, message `1030` was never answered, and the client waited
forever for the third bag — the loading bar stopped at **40%** while the server looked healthy
(a single `FRAMING_A handler err` line in the log).

Now:

- `_load()` backfills **missing** top-level keys from the template (existing values are never
  overwritten) and logs which keys were added, so an old save keeps working after an upgrade;
- `1030` / `1033` use `EQ_STATE.get(...) or {}`, so a save whose key exists but is `null` also
  produces an empty bag instead of an exception.

Same class of failure as v0.1.1's UR-shard bug: the player only sees a stuck progress bar, not an
error, so the server has to be defensive about save shape.

---

## v0.1.1 — 2026-11-14

Server-side fixes for issues found while running the private beta. No client, data tables or
assets are involved — all three changes are pure `work/` code.

### Monster bag: cap raised 21 → 200, delivered as a single page

The official client hard-codes `SQGamer:getMaxMonster() == 21`, but that value is only consumed by
a debug panel; the real capacity is whatever the server puts in the `1032` response's
`"current/total"` page field — the client auto-requests the next page when `current < total`.

**Why the page size equals the cap:** measured on a real client — **each extra page costs a fixed
5 seconds** at login (21 per page with 200 monsters = 10 pages ≈ 50 s stuck on the loading screen).
So `BAG_PAGE` is set to 200: a full bag is a single ~20 KB frame and login time is unchanged.
The paging code (`monster_bag_json(page)` + `{"page": n}` parsing in `game_server.py`) is kept for
future expansion — if you ever set `BAG_PAGE` below `MAX_BAG`, budget roughly 5 s per page.

### Fixed: composing a UR shard could brick an account (login stuck at 80%)

`L07_Item_Compose` maps the UR shard items `4000501` / `4500011` to monster id **500011**, which
exists only in the display table (`L91_StatusRender`) and has no stat row in `L91_Status`.
The old `_add_monster()` silently created it with `skin=0`/`skills=0`; the client cannot resolve
such a monster, throws while parsing `1032`, and every later login hangs at 80% (the fifth bag) —
the save is bricked with no in-game way out.

Now:

- `_add_monster()` refuses ids that are not in the monster table (returns `None`);
- `1168` validates all `ComposeID`s **before** consuming shards (all invalid → error code `114`,
  nothing consumed);
- `1032` skips unknown-id monsters already present in a save, so an affected player can at least log in;
- new `work/_fix_unknown_monsters.py` repairs existing saves (maps `500011 → 500001`, keeps
  ucode/level/exp, dry-run by default, backs up before writing, idempotent).

### Fixed: admin "one-click restore" always returned 400 in multi-account mode

Snapshots only ever covered the template `state.json`; player saves live in
`work/accounts/<aid>.json` and were never snapshotted, so `save_restore()` had to refuse —
overwriting a player's save with the template would destroy it with no way back.

Now:

- every account save is snapshotted automatically **before** the write (5-minute throttle,
  content de-duplication, 10 snapshots kept per account);
- the admin snapshot list / restore operate on account saves; a `pre-restore` copy is taken first;
- after a restore the in-memory account is evicted and online connections are dropped, so the
  client reconnects onto the restored save;
- template snapshots are rejected with a readable explanation, and the web UI shows the server's
  message instead of a bare `400`.

### Tests

Internal regression: 165/165 suites pass. The public subset in `tests/` is unchanged and still
passes (`python tests/run_tests.py`).

---

## v0.1.0 — 2026-11-13

- First public source release of the Yokai: Spirits Hunt (妖怪正传) server core.
- Server code only: no game client, installation package, assets, or data tables.
- Added configurable table directory (`YGZZ_TABLE_DIR` / `YGZZ_TABLE_DIR_ALT`).
- Added a small, self-contained public test subset.

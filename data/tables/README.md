# Data tables

This directory is intentionally empty.

The server reads JSON data tables from `data/tables/` by default, or from the directory
specified by the `YGZZ_TABLE_DIR` environment variable. The tables are **not** part of this
repository and are not distributed here.

Place your own compatible table files in this directory, or point `YGZZ_TABLE_DIR` at the
directory that contains them. Do not commit table files: `data/tables/*` is git-ignored.

The server also supports an optional secondary directory via `YGZZ_TABLE_DIR_ALT`. It is used
for legacy table sets and degrades gracefully when unset.

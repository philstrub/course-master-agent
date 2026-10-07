"""
# Database

Open one of mitsync's DuckDB files, waiting while another mitsync process
holds it.

## 1. What This Module Does

`connect(path, wait)` returns a DuckDB connection to `path`. While another
process holds the file's lock it retries every `RETRY_SECONDS`, and after
`wait` seconds it raises `DatabaseBusy`.

## 2. Why This Module Exists

DuckDB lets one process at a time open a database file, readers included. A
`sync` holds `state/manifest.duckdb` for its whole run (usually under a
minute, sometimes ten) and then rebuilds `state/graph.duckdb`, so every
scheduled job that starts meanwhile -- the filing and graph-build checks, the
agents they wake -- used to die with DuckDB's raw `IOException`. A traceback
is not something a scheduler can act on; "busy, try later" is.

## 3. How It Fits in the Architecture

A `core` leaf used by both stores: `canvas.manifest` and the DuckDB backend in
`knowledge.graph`. Each passes `Settings.lock_wait_seconds`, which commands
lower with `--lock-wait` when they run inside a scheduler's time budget (an
OpenClaw condition check has 30 seconds in all).

## 4. Key Concepts

**Why an exception is caught here.** The lock conflict is a genuinely
external failure: another process's state, not a bug in this one. Only that
conflict is retried and translated into `DatabaseBusy`; any other DuckDB
error raises as it is.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from mitsync.core.errors import DatabaseBusy
from mitsync.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    import duckdb

log = get_logger(__name__)

__all__ = ["DEFAULT_LOCK_WAIT_SECONDS", "connect"]

#: How long a command waits for another process's lock unless told otherwise.
DEFAULT_LOCK_WAIT_SECONDS = 120.0

#: Seconds between attempts while another process holds the lock.
RETRY_SECONDS = 2.0

#: What DuckDB says when another process holds the database file.
_LOCK_CONFLICT = "Could not set lock on file"


def connect(path: Path, wait: float) -> duckdb.DuckDBPyConnection:
    """Open ``path``, retrying for up to ``wait`` seconds while another process holds it."""
    import duckdb  # here, so `config` can import the default without loading DuckDB

    deadline = time.monotonic() + wait
    while True:
        try:
            return duckdb.connect(str(path))
        except duckdb.IOException as exc:
            if _LOCK_CONFLICT not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise DatabaseBusy(
                    f"{path.name} is locked by another mitsync process (usually a sync) "
                    f"and was not released within {wait:.0f}s; run this again when it ends"
                ) from exc
            log.info("%s locked by another process; retrying in %.0fs", path.name, RETRY_SECONDS)
            time.sleep(RETRY_SECONDS)

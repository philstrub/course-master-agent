"""
# Database Tests

`core.database.connect`: a DuckDB file another process holds is waited for,
then reported as `DatabaseBusy` instead of DuckDB's raw lock error.

`holding_lock` takes the lock from a real second process, because DuckDB's
lock is per process: a second connection inside the test process would not
conflict. Both stores open their files through `connect`; the CLI tests check
that the scheduler-facing commands turn `DatabaseBusy` into `{"busy": true}`.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest

from mitsync.core import database
from mitsync.core.errors import DatabaseBusy


@contextmanager
def holding_lock(db: Path) -> Iterator[subprocess.Popen]:
    """A second process with ``db`` open, as a running `sync` has the manifest."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys; c = duckdb.connect(sys.argv[1]); print('ready', flush=True); "
            "sys.stdin.read()",
            str(db),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "ready"
    try:
        yield holder
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)


def test_a_lock_held_past_the_wait_is_database_busy(tmp_path: Path):
    db = tmp_path / "x.duckdb"
    with holding_lock(db), pytest.raises(DatabaseBusy, match="x.duckdb is locked"):
        database.connect(db, 0)


def test_connect_waits_for_the_lock_to_be_released(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "x.duckdb"
    monkeypatch.setattr(database, "RETRY_SECONDS", 0.05)
    with holding_lock(db) as holder:
        sleeps: list[float] = []

        def release_after_first_retry(seconds: float) -> None:
            sleeps.append(seconds)
            holder.stdin.close()
            holder.wait()  # no timeout: Popen polls with the time.sleep patched here

        monkeypatch.setattr(database.time, "sleep", release_after_first_retry)
        con = database.connect(db, 30)
        con.close()
        assert sleeps == [0.05]


def test_any_other_duckdb_error_is_not_retried(tmp_path: Path):
    with pytest.raises(duckdb.IOException):
        database.connect(tmp_path / "missing-dir" / "x.duckdb", 30)

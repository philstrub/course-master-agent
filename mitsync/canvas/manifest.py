"""
# Manifest

The record of what has been mirrored from Canvas, and where it ended up.

## 1. What This Module Does

Owns `state/manifest.duckdb` and the three tables in it:

`files`
    One row per Canvas file, keyed by the Canvas `uuid` (a synthetic
    `canvas-<id>` key is used when Canvas omits one). `mirror_path` is where
    `sync` put the bytes; `filed_path` is where `organize` later copied or
    linked them, and is `NULL` until then. Both are workspace-relative POSIX
    strings. `skip_reason` is set when the agent decided a file is deliberately
    not filed (a pre-class deck once the post-class one exists), so it is not
    offered for filing again. `left_out_reason` is set when the agent looked
    at a file and was unsure: it stays unfiled and listed, but does not wake
    the scheduled filing agent again until its bytes change.
`courses`
    One row per Canvas course seen, keyed by `canvas_id`. `folder` is the
    sanitized mirror folder name under `_canvas/`; the mapping to the
    student's own human-named folder is a separate question and lives in
    `config/courses.yml`.
`runs`
    Append-only log of command invocations with a JSON `stats` blob.

## 2. Why This Module Exists

Every scheduled job in this tool has to be catch-up safe: work is derived from
manifest state versus Canvas state, never from "time since the last run", so a
laptop that was shut for a week resumes correctly instead of skipping the
window it missed. That requires durable state describing what is already
mirrored, and this is it.

It is also what decouples the two halves of the tool. `sync` writes rows;
`organize` reads them to decide what still needs filing and writes back
`filed_path`. Neither has to walk the filesystem to find out what the other
did.

## 3. How It Fits in the Architecture

A storage leaf: it knows about rows, not about Canvas or about filing. `sync`,
`organize`, `deadlines` and `course_map` are the consumers. The database is
*not* a source of truth for file content -- `_canvas/` is, and the mirror can
always be re-synced -- but it is the authority on when a command last ran.

## 4. Key Concepts

**Idempotent open.** The schema is created if absent and left alone if
present; upserts are `INSERT OR REPLACE`.

**`first_seen`, `filed_path` and `skip_reason` are protected on upsert.** A
re-sync must not reset when a file was first seen, and `sync` (which knows
nothing about filing) must not blank the filing state that `organize` owns.

**`left_out_reason` lasts only as long as the bytes.** Upsert keeps it while
`sha256` is unchanged and clears it when Canvas has a new version: the agent
left out the old file, not this one.

**Columns added later are added in place.** `skip_reason` and
`left_out_reason` arrived after the first manifests were written, so opening
one adds them with `ADD COLUMN IF NOT EXISTS` rather than asking for a rebuild.

**One process at a time, and the others wait.** DuckDB lets a single process
hold the database file, so a `sync` that runs for minutes locks out `unfiled`,
`organize` and `due` for as long. Opening retries on that lock for up to
`wait` seconds (`DEFAULT_WAIT_SECONDS` unless the caller says otherwise), then
raises `ManifestBusy` instead of DuckDB's own error, so a scheduled caller can
tell "sync is running, come back later" from a real failure.

**Runs are append-only.** `last_run` answers sync-freshness questions for
`mitsync due` and for `_kb/AGENTS.md`, so both read the same authority and can
never disagree.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import duckdb

from mitsync.core.errors import ManifestBusy
from mitsync.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["DEFAULT_WAIT_SECONDS", "CourseRecord", "FileRecord", "Manifest", "synthetic_uuid"]

#: How long opening the manifest waits for another process's lock. A sync
#: usually holds it for under a minute; a slow one, for several.
DEFAULT_WAIT_SECONDS = 120.0

#: Seconds between attempts while another process holds the lock.
_RETRY_SECONDS = 2.0

#: What DuckDB says when another process holds the database file.
_LOCK_CONFLICT = "Could not set lock on file"


@dataclass
class FileRecord:
    """One mirrored Canvas file."""

    uuid: str
    canvas_id: int
    course_folder: str
    course_canvas_id: int
    display_name: str
    filename: str
    content_type: str | None
    size: int | None
    canvas_folder: str | None
    module_name: str | None
    module_position: int | None
    updated_at: str
    sha256: str
    mirror_path: str
    filed_path: str | None
    first_seen: str
    last_synced: str
    skip_reason: str | None = None
    left_out_reason: str | None = None


@dataclass
class CourseRecord:
    """One Canvas course and the mirror folder it maps onto."""

    canvas_id: int
    name: str
    course_code: str | None
    term_name: str | None
    term_start: str | None
    term_end: str | None
    folder: str


def synthetic_uuid(canvas_id: int | str) -> str:
    """Stable stand-in key for a file whose Canvas record has no ``uuid``."""
    return f"canvas-{canvas_id}"


_FILE_COLUMNS = [f.name for f in fields(FileRecord)]
_COURSE_COLUMNS = [f.name for f in fields(CourseRecord)]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    uuid             VARCHAR PRIMARY KEY,
    canvas_id        BIGINT,
    course_folder    VARCHAR,
    course_canvas_id BIGINT,
    display_name     VARCHAR,
    filename         VARCHAR,
    content_type     VARCHAR,
    size             BIGINT,
    canvas_folder    VARCHAR,
    module_name      VARCHAR,
    module_position  INTEGER,
    updated_at       VARCHAR,
    sha256           VARCHAR,
    mirror_path      VARCHAR,
    filed_path       VARCHAR,
    first_seen       VARCHAR,
    last_synced      VARCHAR,
    skip_reason      VARCHAR,
    left_out_reason  VARCHAR
);

ALTER TABLE files ADD COLUMN IF NOT EXISTS skip_reason VARCHAR;
ALTER TABLE files ADD COLUMN IF NOT EXISTS left_out_reason VARCHAR;

CREATE TABLE IF NOT EXISTS courses (
    canvas_id   BIGINT PRIMARY KEY,
    name        VARCHAR,
    course_code VARCHAR,
    term_name   VARCHAR,
    term_start  VARCHAR,
    term_end    VARCHAR,
    folder      VARCHAR
);

CREATE TABLE IF NOT EXISTS runs (
    command  VARCHAR,
    started  VARCHAR,
    finished VARCHAR,
    stats    VARCHAR
);
"""


class Manifest:
    """CRUD over the DuckDB manifest. Usable as a context manager."""

    def __init__(self, db_path: Path, *, wait: float = DEFAULT_WAIT_SECONDS) -> None:
        self.db_path = Path(db_path)
        self._con = _connect(self.db_path, wait)
        self._con.execute(_SCHEMA)

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> Manifest:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None  # type: ignore[assignment]

    # -- files -------------------------------------------------------------
    def upsert_file(self, rec: FileRecord) -> None:
        """Insert or replace a file row, preserving its original ``first_seen``.

        ``filed_path``, ``skip_reason`` and ``left_out_reason`` are owned by
        ``organize.py``; a record carrying ``None`` never clobbers a value
        already stored, except that new bytes clear ``left_out_reason``.
        """
        existing = self.get_file(rec.uuid)
        if existing is not None:
            if existing.first_seen:
                rec.first_seen = existing.first_seen
            if rec.filed_path is None:
                rec.filed_path = existing.filed_path
            if rec.skip_reason is None:
                rec.skip_reason = existing.skip_reason
            if rec.left_out_reason is None and rec.sha256 == existing.sha256:
                rec.left_out_reason = existing.left_out_reason
        values = [getattr(rec, c) for c in _FILE_COLUMNS]
        placeholders = ", ".join("?" for _ in _FILE_COLUMNS)
        self._con.execute(
            f"INSERT OR REPLACE INTO files ({', '.join(_FILE_COLUMNS)}) VALUES ({placeholders})",
            values,
        )

    def get_file(self, uuid: str) -> FileRecord | None:
        row = self._con.execute(
            f"SELECT {', '.join(_FILE_COLUMNS)} FROM files WHERE uuid = ?", [uuid]
        ).fetchone()
        return FileRecord(*row) if row else None

    def list_files(
        self, course: str | None = None, *, unfiled_only: bool = False
    ) -> list[FileRecord]:
        """All file rows, newest-first by ``last_synced``.

        ``course`` matches the mirror folder name; ``unfiled_only`` restricts to
        rows ``organize.py`` has neither filed nor skipped.
        """
        sql = f"SELECT {', '.join(_FILE_COLUMNS)} FROM files"
        clauses: list[str] = []
        params: list[Any] = []
        if course is not None:
            clauses.append("course_folder = ?")
            params.append(course)
        if unfiled_only:
            clauses.append("filed_path IS NULL AND skip_reason IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_synced DESC, mirror_path"
        return [FileRecord(*r) for r in self._con.execute(sql, params).fetchall()]

    def set_filed_path(self, uuid: str, filed_path: str | None) -> None:
        self._con.execute("UPDATE files SET filed_path = ? WHERE uuid = ?", [filed_path, uuid])

    def set_skip_reason(self, uuid: str, skip_reason: str | None) -> None:
        self._con.execute("UPDATE files SET skip_reason = ? WHERE uuid = ?", [skip_reason, uuid])

    def set_left_out_reason(self, uuid: str, reason: str | None) -> None:
        self._con.execute("UPDATE files SET left_out_reason = ? WHERE uuid = ?", [reason, uuid])

    # -- courses -----------------------------------------------------------
    def upsert_course(self, rec: CourseRecord) -> None:
        values = [getattr(rec, c) for c in _COURSE_COLUMNS]
        placeholders = ", ".join("?" for _ in _COURSE_COLUMNS)
        self._con.execute(
            f"INSERT OR REPLACE INTO courses ({', '.join(_COURSE_COLUMNS)}) "
            f"VALUES ({placeholders})",
            values,
        )

    def list_courses(self) -> list[CourseRecord]:
        rows = self._con.execute(
            f"SELECT {', '.join(_COURSE_COLUMNS)} FROM courses ORDER BY folder"
        ).fetchall()
        return [CourseRecord(*row) for row in rows]

    # -- runs --------------------------------------------------------------
    def record_run(self, command: str, started: str, finished: str, stats: dict) -> None:
        self._con.execute(
            "INSERT INTO runs (command, started, finished, stats) VALUES (?, ?, ?, ?)",
            [command, started, finished, json.dumps(stats, default=str)],
        )

    def last_run(self, command: str) -> dict | None:
        """The most recent run of ``command`` as a plain dict, or ``None``."""
        row = self._con.execute(
            "SELECT command, started, finished, stats FROM runs "
            "WHERE command = ? ORDER BY finished DESC, rowid DESC LIMIT 1",
            [command],
        ).fetchone()
        if row is None:
            return None
        stats = json.loads(row[3]) if row[3] else {}
        return {"command": row[0], "started": row[1], "finished": row[2], "stats": stats}


def _connect(db_path: Path, wait: float) -> duckdb.DuckDBPyConnection:
    """Open the database, retrying while another process holds its lock."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return duckdb.connect(str(db_path))
        except duckdb.IOException as exc:  # external: another process holds the file
            if _LOCK_CONFLICT not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise ManifestBusy(
                    f"{db_path.name} is locked by another mitsync process (usually a sync) "
                    f"and was not released within {wait:.0f}s; run this again when it ends"
                ) from exc
            log.info("manifest locked by another process; retrying in %.0fs", _RETRY_SECONDS)
            time.sleep(_RETRY_SECONDS)

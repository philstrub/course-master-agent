"""DuckDB manifest: the record of what has been mirrored from Canvas.

Three tables live in `state/manifest.duckdb`:

``files``
    One row per Canvas file, keyed by the Canvas ``uuid`` (a synthetic
    ``canvas-<id>`` key is used when Canvas omits one). ``mirror_path`` is where
    :mod:`mitsync.sync` put the bytes; ``filed_path`` is where
    :mod:`mitsync.organize` later copied/linked them (``NULL`` until then).
    Both are workspace-relative POSIX strings.
``courses``
    One row per Canvas course seen, keyed by ``canvas_id``. ``folder`` is the
    sanitized mirror folder name under ``_canvas/``; the mapping to the
    student's own human-named folder lives in ``config/courses.yml``.
``runs``
    Append-only log of command invocations with a JSON ``stats`` blob, so jobs
    can be catch-up safe (work is derived from manifest state, never from
    "time since last run").

Opening is idempotent: the schema is created if absent and left alone if
present. Upserts are delete-then-insert semantics via ``INSERT OR REPLACE``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import duckdb

from .logging import get_logger

log = get_logger(__name__)

__all__ = ["CourseRecord", "FileRecord", "Manifest", "synthetic_uuid"]


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
    last_synced      VARCHAR
);

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

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self.db_path))
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

        ``filed_path`` is owned by ``organize.py``; a record carrying ``None``
        never clobbers a path already stored.
        """
        existing = self.get_file(rec.uuid)
        if existing is not None:
            if existing.first_seen:
                rec.first_seen = existing.first_seen
            if rec.filed_path is None:
                rec.filed_path = existing.filed_path
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
        return _to_file(row) if row else None

    def list_files(
        self, course: str | None = None, *, unfiled_only: bool = False
    ) -> list[FileRecord]:
        """All file rows, newest-first by ``last_synced``.

        ``course`` matches the mirror folder name; ``unfiled_only`` restricts to
        rows ``organize.py`` has not filed yet.
        """
        sql = f"SELECT {', '.join(_FILE_COLUMNS)} FROM files"
        clauses: list[str] = []
        params: list[Any] = []
        if course is not None:
            clauses.append("course_folder = ?")
            params.append(course)
        if unfiled_only:
            clauses.append("filed_path IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_synced DESC, mirror_path"
        return [_to_file(r) for r in self._con.execute(sql, params).fetchall()]

    def set_filed_path(self, uuid: str, filed_path: str | None) -> None:
        self._con.execute("UPDATE files SET filed_path = ? WHERE uuid = ?", [filed_path, uuid])

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
        try:
            stats = json.loads(row[3]) if row[3] else {}
        except json.JSONDecodeError:  # pragma: no cover - defensive
            log.warning("run stats for %s are not valid JSON", command)
            stats = {}
        return {"command": row[0], "started": row[1], "finished": row[2], "stats": stats}

    # -- convenience -------------------------------------------------------
    def iter_files(self) -> Iterator[FileRecord]:
        yield from self.list_files()


def _to_file(row: tuple) -> FileRecord:
    return FileRecord(*row)

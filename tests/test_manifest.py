"""
# Manifest Tests

The DuckDB schema, upsert semantics, and the two fields upsert must not
clobber.

Covers the round trip for file and course rows, filtered listing (by course
and by unfiled-only), the append-only `runs` log and its per-command
`last_run`, and `synthetic_uuid` for files Canvas hands over without one.

The load-bearing cases are the ownership rules between modules: `first_seen`
survives a re-sync, and a `filed_path` written by `organize` is not blanked by
a later `sync` upsert that carries `None`. Those two are why `upsert_file`
reads the existing row first, and a regression in either silently rewrites
history or unfiles the student's material.

`left_out_reason` lives only as long as the file's bytes. Waiting for a lock
another process holds is `core.database`'s, tested in `test_database.py`.

`db` is a local fixture -- a fresh `manifest.duckdb` under `tmp_path`. These
tests need no workspace and do not use the `settings` fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mitsync.canvas.manifest import CourseRecord, FileRecord, Manifest, synthetic_uuid


def make_file(uuid: str = "uuid-1", **over) -> FileRecord:
    base = dict(
        uuid=uuid,
        canvas_id=5001,
        course_folder="Machine Learning",
        course_canvas_id=28451,
        display_name="Lecture 1.pdf",
        filename="lecture1.pdf",
        content_type="application/pdf",
        size=21,
        canvas_folder="Lectures",
        module_name="Week 1 - Foundations",
        module_position=1,
        updated_at="2026-09-01T10:00:00Z",
        sha256="a" * 64,
        mirror_path="_canvas/Machine Learning/Lectures/lecture1.pdf",
        filed_path=None,
        first_seen="2026-09-02T00:00:00+00:00",
        last_synced="2026-09-02T00:00:00+00:00",
    )
    base.update(over)
    return FileRecord(**base)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """The manifest path inside a state/ dir, as `Paths.ensure()` would leave it."""
    state = tmp_path / "state"
    state.mkdir()
    return state / "manifest.duckdb"


def test_open_creates_schema_and_is_idempotent(db: Path):
    with Manifest(db) as m:
        assert m.list_files() == []
        assert m.list_courses() == []
    assert db.exists()
    # Re-opening an existing DB must not blow up or lose data.
    with Manifest(db) as m:
        m.upsert_file(make_file())
    with Manifest(db) as m:
        assert len(m.list_files()) == 1


def test_file_round_trip(db: Path):
    rec = make_file()
    with Manifest(db) as m:
        m.upsert_file(rec)
        got = m.get_file("uuid-1")
    assert got == rec


def test_upsert_is_idempotent_and_updates_in_place(db: Path):
    with Manifest(db) as m:
        m.upsert_file(make_file())
        m.upsert_file(make_file())
        assert len(m.list_files()) == 1

        m.upsert_file(make_file(updated_at="2026-10-01T00:00:00Z", sha256="b" * 64))
        rows = m.list_files()
        assert len(rows) == 1
        assert rows[0].updated_at == "2026-10-01T00:00:00Z"
        assert rows[0].sha256 == "b" * 64


def test_upsert_preserves_first_seen_and_filed_path(db: Path):
    with Manifest(db) as m:
        m.upsert_file(make_file())
        m.set_filed_path("uuid-1", "Machine Learning/lectures/Lecture 1.pdf")
        m.upsert_file(make_file(first_seen="2099-01-01T00:00:00+00:00"))
        got = m.get_file("uuid-1")
    assert got is not None
    assert got.first_seen == "2026-09-02T00:00:00+00:00"
    assert got.filed_path == "Machine Learning/lectures/Lecture 1.pdf"


def test_get_file_missing_returns_none(db: Path):
    with Manifest(db) as m:
        assert m.get_file("nope") is None


def test_list_files_filters(db: Path):
    with Manifest(db) as m:
        m.upsert_file(make_file("uuid-1"))
        m.upsert_file(
            make_file("uuid-2", canvas_id=6001, course_folder="Algorithms", mirror_path="x.pdf")
        )
        m.set_filed_path("uuid-2", "Algorithms/x.pdf")

        assert {f.uuid for f in m.list_files()} == {"uuid-1", "uuid-2"}
        assert [f.uuid for f in m.list_files(course="Algorithms")] == ["uuid-2"]
        assert [f.uuid for f in m.list_files(unfiled_only=True)] == ["uuid-1"]
        assert m.list_files(course="Algorithms", unfiled_only=True) == []


def test_set_filed_path_can_clear(db: Path):
    with Manifest(db) as m:
        m.upsert_file(make_file())
        m.set_filed_path("uuid-1", "somewhere.pdf")
        m.set_filed_path("uuid-1", None)
        assert m.get_file("uuid-1").filed_path is None


def test_course_round_trip_and_upsert(db: Path):
    rec = CourseRecord(
        canvas_id=28451,
        name="Machine Learning",
        course_code="6.7900",
        term_name="2026 Fall",
        term_start="2026-09-01T04:00:00Z",
        term_end="2026-12-20T04:00:00Z",
        folder="Machine Learning",
    )
    with Manifest(db) as m:
        m.upsert_course(rec)
        m.upsert_course(rec)
        assert m.list_courses() == [rec]

        renamed = CourseRecord(**{**rec.__dict__, "name": "Machine Learning (F26)"})
        m.upsert_course(renamed)
        assert m.list_courses() == [renamed]


def test_runs_log(db: Path):
    with Manifest(db) as m:
        assert m.last_run("sync") is None
        m.record_run("sync", "2026-09-01T00:00:00+00:00", "2026-09-01T00:01:00+00:00", {"new": 1})
        m.record_run("sync", "2026-09-02T00:00:00+00:00", "2026-09-02T00:01:00+00:00", {"new": 4})
        last = m.last_run("sync")
    assert last is not None
    assert last["command"] == "sync"
    assert last["stats"]["new"] == 4
    assert last["finished"] == "2026-09-02T00:01:00+00:00"


def test_last_run_is_per_command(db: Path):
    with Manifest(db) as m:
        m.record_run("sync", "a", "b", {})
        assert m.last_run("organize") is None


def test_synthetic_uuid():
    assert synthetic_uuid(42) == "canvas-42"


def test_left_out_survives_a_resync_until_the_bytes_change(db: Path):
    with Manifest(db) as m:
        m.upsert_file(make_file())
        m.set_left_out_reason("uuid-1", "lecture or recitation?")
        m.upsert_file(make_file())
        assert m.get_file("uuid-1").left_out_reason == "lecture or recitation?"
        m.upsert_file(make_file(sha256="b" * 64))
        assert m.get_file("uuid-1").left_out_reason is None

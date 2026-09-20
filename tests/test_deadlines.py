"""deadlines.py (due.json + briefing) and notify.py."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from mitsync import deadlines, notify
from mitsync.config import Settings
from mitsync.manifest import FileRecord, Manifest

MIRROR = "Machine Learning 15.095"


def _in(days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds")


def write_meta(path: Path, course_canvas_id: int | None, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fetched_at": _in(0), "course_canvas_id": course_canvas_id, "items": items})
    )


@pytest.fixture
def seeded(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """A workspace with Canvas metadata on disk and no calendar CLI on PATH."""
    monkeypatch.setenv("PATH", str(tmp_path / "definitely-empty"))
    (settings.paths.config_dir).mkdir(parents=True, exist_ok=True)
    (settings.paths.config_dir / "courses.yml").write_text(
        yaml.safe_dump(
            {
                "courses": [
                    {
                        "canvas_id": 1,
                        "folder": "Machine Learning",
                        "course_number": "15.095",
                        "aliases": ["ML"],
                    }
                ]
            }
        )
    )
    mirror = settings.paths.canvas_mirror / MIRROR
    write_meta(
        mirror / "_meta" / "courses.json",
        1,
        [{"id": 1, "name": "Machine Learning", "mirror_folder": MIRROR}],
    )
    write_meta(
        mirror / "_meta" / "assignments.json",
        1,
        [
            {
                "id": 88,
                "name": "Problem Set 1",
                "due_at": _in(2),
                "html_url": "https://canvas.mit.edu/courses/1/assignments/88",
                "submission": {"submitted_at": None},
            },
            {
                "id": 89,
                "name": "Problem Set 0",
                "due_at": _in(-3),
                "html_url": "https://canvas.mit.edu/courses/1/assignments/89",
                "submission": {"submitted_at": _in(-4)},
            },
            {"id": 90, "name": "Ungraded reading", "due_at": None},
        ],
    )
    write_meta(
        settings.paths.canvas_mirror / "_meta" / "planner.json",
        None,
        [
            {
                "course_id": 1,
                "plannable_type": "quiz",
                "plannable_date": _in(4),
                "plannable": {"id": 5, "title": "Quiz 2"},
                "html_url": "/courses/1/quizzes/5",
                "submissions": {"submitted": False},
            }
        ],
    )
    return settings


def seed_manifest(settings: Settings, *, run: bool = True) -> None:
    with Manifest(settings.paths.manifest_db) as man:
        man.upsert_file(
            FileRecord(
                uuid="u1",
                canvas_id=101,
                course_folder=MIRROR,
                course_canvas_id=1,
                display_name="Lec03_2026.pdf",
                filename="Lec03_2026.pdf",
                content_type="application/pdf",
                size=10,
                canvas_folder="course files",
                module_name="Week 3",
                module_position=3,
                updated_at=_in(-1),
                sha256="abc",
                mirror_path=f"_canvas/{MIRROR}/Lec03_2026.pdf",
                filed_path=None,
                first_seen=_in(-1),
                last_synced=_in(-1),
            )
        )
        if run:
            man.record_run("sync", _in(-1), _in(-1), {"new": 1})


# --------------------------------------------------------------------------
# due.json
# --------------------------------------------------------------------------
def test_build_due_merges_planner_and_assignments(seeded: Settings) -> None:
    report = deadlines.build_due(seeded)

    path = seeded.paths.kb / "due.json"
    assert path.exists()
    doc = json.loads(path.read_text())
    titles = [i["title"] for i in doc["items"]]
    assert "Problem Set 1" in titles and "Quiz 2" in titles and "Ungraded reading" in titles

    by_title = {i["title"]: i for i in doc["items"]}
    assert by_title["Problem Set 1"]["course"] == "Machine Learning"
    assert by_title["Problem Set 1"]["source"] == "canvas/assignments"
    assert by_title["Problem Set 1"]["submitted"] is False
    assert by_title["Problem Set 0"]["submitted"] is True
    assert by_title["Quiz 2"]["type"] == "quiz"
    assert set(by_title["Quiz 2"]) == {
        "course",
        "title",
        "due_at",
        "type",
        "url",
        "source",
        "submitted",
    }
    assert report.calendar_ok is False


def test_due_items_are_sorted_by_date_with_undated_last(seeded: Settings) -> None:
    items = deadlines.build_due(seeded).items
    dated = [i["due_at"] for i in items if i["due_at"]]
    assert dated == sorted(dated)
    assert items[-1]["due_at"] is None


def test_build_due_survives_missing_meta_files(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    report = deadlines.build_due(settings)
    assert report.items == []
    assert any("planner" in w for w in report.warnings)
    assert (settings.paths.kb / "due.json").exists()


def test_build_due_notes_a_missing_sync(seeded: Settings) -> None:
    report = deadlines.build_due(seeded)
    assert report.last_sync is None
    assert any("sync" in w for w in report.warnings)


def test_build_due_includes_tagged_calendar_events(
    seeded: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_calendar import fake_cli

    payload = {
        "events": [
            {
                "title": "15.095 Machine Learning Lecture",
                "startDate": _in(1),
                "endDate": _in(1.1),
                "calendar": "MIT",
            },
            {"title": "Dentist", "startDate": _in(1), "endDate": _in(1.1), "calendar": "Personal"},
        ]
    }
    fake_cli(tmp_path, monkeypatch, stdout=json.dumps(payload))

    report = deadlines.build_due(seeded)

    assert report.calendar_ok is True
    calendar_items = [i for i in report.items if i["source"] == "calendar"]
    assert len(calendar_items) == 1
    assert calendar_items[0]["course"] == "Machine Learning"


def test_build_due_reports_a_denied_calendar_without_crashing(
    seeded: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_calendar import fake_cli

    fake_cli(tmp_path, monkeypatch, stderr="access denied", exit_code=1)
    report = deadlines.build_due(seeded)
    assert report.calendar_ok is False
    assert any("denied" in w for w in report.warnings)
    assert report.items, "Canvas deadlines still appear when the calendar is unavailable"


# --------------------------------------------------------------------------
# briefing
# --------------------------------------------------------------------------
def test_briefing_names_the_last_successful_sync(seeded: Settings) -> None:
    seed_manifest(seeded)
    path = deadlines.write_briefing(seeded)
    text = path.read_text()

    assert path.name == f"{datetime.now(UTC).strftime('%Y-%m-%d')}.md"
    assert "Last successful sync" in text
    with Manifest(seeded.paths.manifest_db) as man:
        stamp = man.last_run("sync")["finished"]
    assert stamp in text


def test_briefing_shouts_when_no_sync_has_ever_run(seeded: Settings) -> None:
    text = deadlines.write_briefing(seeded).read_text()
    assert "No successful sync recorded yet" in text


def test_briefing_lists_due_items_and_new_materials(seeded: Settings) -> None:
    seed_manifest(seeded)
    text = deadlines.write_briefing(seeded).read_text()

    assert "Problem Set 1" in text
    assert "Quiz 2" in text
    assert "Lec03_2026.pdf" in text
    assert "Problem Set 0" not in text, "items due days ago are out of the window"


def test_briefing_says_so_when_the_calendar_is_unavailable(seeded: Settings) -> None:
    text = deadlines.write_briefing(seeded).read_text()
    assert "Calendar unavailable" in text
    assert "Gaps in this briefing" in text


def test_briefing_is_catch_up_safe(seeded: Settings) -> None:
    """Two runs a week apart both derive from stored state, not elapsed time."""
    seed_manifest(seeded)
    first = deadlines.write_briefing(seeded).read_text()
    second = deadlines.write_briefing(seeded).read_text()
    assert first.split("_Generated")[0] == second.split("_Generated")[0]


# --------------------------------------------------------------------------
# notify
# --------------------------------------------------------------------------
def test_notify_is_a_no_op_when_silenced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MITSYNC_NO_NOTIFY", "1")
    assert notify.notify("title", "message") is False


def test_notify_never_raises_without_a_notifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MITSYNC_NO_NOTIFY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert notify.notify("title", "message", subtitle="sub") is False


def test_notify_never_raises_when_the_notifier_explodes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MITSYNC_NO_NOTIFY", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "terminal-notifier"
    script.write_text("#!/bin/sh\nexit 3\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    assert notify.notify('he said "hi"', "line\nbreak") is False


def test_notify_uses_terminal_notifier_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MITSYNC_NO_NOTIFY", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = bindir / "argv.txt"
    script = bindir / "terminal-notifier"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\nexit 0\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    assert notify.notify("mitsync", "2 items due", subtitle="today") is True
    argv = log.read_text().splitlines()
    assert "-title" in argv and "mitsync" in argv and "today" in argv


def test_applescript_quoting_escapes_quotes_and_backslashes() -> None:
    assert notify._applescript_quote('a "b" \\ c') == 'a \\"b\\" \\\\ c'
    assert "\n" not in notify._applescript_quote("a\nb")

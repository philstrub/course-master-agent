"""course_map.py: reading config/courses.yml, and which folders are courses."""

from __future__ import annotations

import pytest

from mitsync.core.config import Settings
from mitsync.core.errors import ConfigError
from mitsync.filing import course_map
from tests.test_organize import write_course_map


def test_load_course_map_reads_entries(settings: Settings) -> None:
    write_course_map(
        settings,
        [{"canvas_id": 1, "folder": "Machine Learning", "course_number": "15.095", "aliases": []}],
    )
    entries = course_map.load_course_map(settings)
    assert entries == [
        {"canvas_id": 1, "folder": "Machine Learning", "course_number": "15.095", "aliases": []}
    ]
    assert course_map.folder_for_canvas_id(settings, 1) == "Machine Learning"
    assert course_map.folder_for_canvas_id(settings, "1") == "Machine Learning"
    assert course_map.folder_for_canvas_id(settings, 2) is None


def test_missing_course_map_is_empty(settings: Settings) -> None:
    course_map.courses_file(settings).unlink()
    assert course_map.load_course_map(settings) == []
    assert course_map.existing_course_folders(settings) == []


def test_malformed_course_map_raises_naming_the_file(settings: Settings) -> None:
    course_map.courses_file(settings).write_text("courses: {not: a list}\n")
    with pytest.raises(ConfigError, match="courses.yml"):
        course_map.load_course_map(settings)


def test_only_mapped_folders_that_exist_are_course_folders(settings: Settings) -> None:
    ws = settings.paths.workspace
    (ws / "memory").mkdir()  # OpenClaw's, not a course
    (ws / "Optimization").mkdir()
    write_course_map(
        settings,
        [
            {"canvas_id": 1, "folder": "Machine Learning"},
            {"canvas_id": 2, "folder": "Optimization"},
            {"canvas_id": 3, "folder": "Not On Disk Yet"},
        ],
    )
    assert course_map.existing_course_folders(settings) == ["Machine Learning", "Optimization"]


def test_deadlines_no_longer_drags_in_organize() -> None:
    """The whole point of the split: the light readers stay light."""
    import subprocess
    import sys

    probe = (
        "import sys, mitsync.schedule.deadlines, mitsync.schedule.calendar; "
        "print('mitsync.filing.organize' in sys.modules)"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"

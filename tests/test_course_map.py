"""course_map.py: config/courses.yml, and the evidence `mitsync map` proposes it from."""

from __future__ import annotations

import pytest

from mitsync.core.config import Settings
from mitsync.filing import course_map
from mitsync.llm.rules_driver import RulesJudge
from tests.test_organize import add_mirror_file, write_course_map, write_naming


@pytest.fixture
def prepared(settings: Settings) -> Settings:
    write_naming(settings)
    write_course_map(
        settings,
        [{"canvas_id": 1, "folder": "Machine Learning", "course_number": "15.095", "aliases": []}],
    )
    return settings


def test_suggest_course_map_writes_nothing_without_apply(settings: Settings) -> None:
    write_naming(settings)
    add_mirror_file(settings, uuid="u1", canvas_id=101, name="15_095_hw1.pdf")

    report = course_map.suggest_course_map(settings, RulesJudge(settings))

    assert report.written is False
    assert not (settings.paths.config_dir / "courses.yml").exists()
    assert report.mappings and report.mappings[0]["folder"] == "Machine Learning"
    assert "courses:" in report.yaml_text


def test_suggest_course_map_apply_writes_and_preserves_hand_edits(settings: Settings) -> None:
    write_naming(settings)
    add_mirror_file(settings, uuid="u1", canvas_id=101, name="15_095_hw1.pdf")
    write_course_map(
        settings,
        [{"canvas_id": 1, "folder": "My Own Name", "course_number": "15.095", "aliases": ["ML"]}],
    )

    report = course_map.suggest_course_map(settings, RulesJudge(settings), apply=True)

    assert report.written is True
    entries = course_map.load_course_map(settings)
    assert entries[0]["folder"] == "My Own Name", "a hand-edited folder must survive"
    assert entries[0]["aliases"] == ["ML"]
    assert report.conflicts and report.conflicts[0]["proposed_folder"] == "Machine Learning"
    assert 1 in report.preserved and not report.added


def test_suggest_course_map_adds_new_courses(settings: Settings) -> None:
    write_naming(settings)
    add_mirror_file(settings, uuid="u1", canvas_id=101, name="a.pdf")
    add_mirror_file(
        settings, uuid="u2", canvas_id=102, name="b.pdf", mirror_folder="AI", course_canvas_id=2
    )
    write_course_map(settings, [{"canvas_id": 1, "folder": "Machine Learning", "aliases": []}])

    report = course_map.suggest_course_map(settings, RulesJudge(settings), apply=True)

    assert report.added == [2]
    assert {e["canvas_id"] for e in course_map.load_course_map(settings)} == {1, 2}


def test_observed_course_numbers_are_mined_from_filenames(prepared: Settings) -> None:
    path = prepared.paths.workspace / "Machine Learning" / "15_095_hw1.pdf"
    path.write_text("x")
    assert course_map.observed_course_numbers(prepared) == {"15.095": ["Machine Learning"]}


def test_suggest_course_map_without_any_canvas_courses(settings: Settings) -> None:
    write_naming(settings)
    report = course_map.suggest_course_map(settings, RulesJudge(settings), apply=True)
    assert report.mappings == [] and report.written is False


def test_a_bare_year_is_not_a_course_number() -> None:
    """`Fall_2026` must not become the course "20.26" (rules_driver used to let it)."""
    assert course_map.course_numbers_in("deliv_1_15072_Fall2026.pdf") == ["15.072"]
    assert course_map.course_numbers_in("Fall_2026") == []
    assert course_map.course_numbers_in("15.C57 syllabus") == ["15.C57"]


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

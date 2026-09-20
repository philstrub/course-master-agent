"""Course selection: dateless terms, explicit terms, and `canvas.exclude_courses`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from mitsync.config import Settings, load_settings
from mitsync.organize import load_course_map
from mitsync.paths import Paths
from mitsync.sync import (
    SyncReport,
    course_exclusion_reason,
    filter_excluded_courses,
    select_current_term_courses,
)

_TODAY = datetime.now(UTC).date()
CURRENT_TERM = {
    "id": 42,
    "name": "Fall Term (AY 2026-2027)",
    "start_at": (_TODAY - timedelta(days=20)).isoformat() + "T04:00:00Z",
    "end_at": (_TODAY + timedelta(days=90)).isoformat() + "T04:00:00Z",
}
OLD_TERM = {
    "id": 7,
    "name": "Spring Term (AY 2023-2024)",
    "start_at": "2024-02-05T05:00:00Z",
    "end_at": "2024-05-20T05:00:00Z",
}
DEFAULT_TERM = {"id": 1, "name": "Default Term", "start_at": None, "end_at": None}

DATED = {
    "id": 40577,
    "name": "MAS.665 Foundations of AI Ventures",
    "course_code": "MAS.665",
    "term": CURRENT_TERM,
}
DATELESS = {
    "id": 17557,
    "name": "F-1 Immigration Orientation eCourse for New-Incoming International Students",
    "course_code": "F1-ORIENT",
    "term": DEFAULT_TERM,
}
STALE = {"id": 11111, "name": "Ancient History", "course_code": "21H.001", "term": OLD_TERM}


# --------------------------------------------------------------------------
# the dateless-term rule
# --------------------------------------------------------------------------
def test_dateless_term_is_dropped_when_a_dated_current_course_exists() -> None:
    kept, dropped = select_current_term_courses([DATED, DATELESS, STALE])

    assert [c["id"] for c in kept] == [40577]
    assert dropped == [
        {
            "canvas_id": "17557",
            "name": DATELESS["name"],
            "reason": "term has no start/end dates",
        }
    ]


def test_dateless_term_is_kept_when_no_course_has_dates() -> None:
    other = {"id": 999, "name": "Onboarding", "term": {"name": "Default Term"}}
    kept, dropped = select_current_term_courses([DATELESS, other])

    assert [c["id"] for c in kept] == [17557, 999]
    assert dropped == []


def test_stale_terms_alone_never_yield_an_empty_list_from_dateless_courses() -> None:
    """Only out-of-window dated courses: they stay excluded, dateless ones stay."""
    kept, dropped = select_current_term_courses([STALE, DATELESS])
    assert [c["id"] for c in kept] == [17557]
    assert dropped == []


def test_explicit_term_name_still_matches_by_name() -> None:
    kept, dropped = select_current_term_courses(
        [DATED, DATELESS, STALE], "fall term (AY 2026-2027)"
    )
    assert [c["id"] for c in kept] == [40577]
    assert dropped == []


def test_explicit_term_name_can_select_the_default_term() -> None:
    """An explicit name wins outright; the dateless rule does not apply."""
    kept, _ = select_current_term_courses([DATED, DATELESS], "Default Term")
    assert [c["id"] for c in kept] == [17557]


# --------------------------------------------------------------------------
# exclude_courses
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pattern",
    [17557, "17557", "f-1 immigration", "F-1 IMMIGRATION ORIENTATION", "f1-orient", "F1-Orient"],
)
def test_exclude_courses_matches_id_and_substrings(pattern: object) -> None:
    assert course_exclusion_reason(DATELESS, [pattern]) == "excluded by config"


@pytest.mark.parametrize("pattern", ["1755", "immigration policy", 40577, "", "   "])
def test_exclude_courses_does_not_over_match(pattern: object) -> None:
    assert course_exclusion_reason(DATELESS, [pattern]) is None


def test_exclusion_reason_accepts_mapped_rows_with_canvas_id() -> None:
    row = {"canvas_id": 17557, "name": "F-1 Orientation", "course_code": None}
    assert course_exclusion_reason(row, ["17557"]) == "excluded by config"


def test_filter_excluded_courses_splits_and_reports() -> None:
    kept, dropped = filter_excluded_courses([DATED, DATELESS], ["f-1 immigration"])

    assert [c["id"] for c in kept] == [40577]
    assert dropped == [
        {"canvas_id": "17557", "name": DATELESS["name"], "reason": "excluded by config"}
    ]


def test_report_records_exclusions() -> None:
    report = SyncReport()
    report.add_excluded(17557, "F-1 Orientation", "excluded by config")

    assert report.excluded == [
        {"canvas_id": "17557", "name": "F-1 Orientation", "reason": "excluded by config"}
    ]
    assert report.as_dict()["excluded"] == report.excluded
    assert "17557" in _render(report)


def _render(report: SyncReport) -> str:
    from rich.console import Console

    console = Console(record=True, width=200)
    console.print(report.render())
    return console.export_text()


# --------------------------------------------------------------------------
# the shipped config/courses.yml
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_shipped_courses_yml_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The seeded file must parse in exactly the shape `load_course_map` expects."""
    ws = tmp_path / "courses"
    (ws / "_agent" / "config").mkdir(parents=True)
    (ws / "_agent" / "config" / "courses.yml").write_text(
        (REPO_ROOT / "config" / "courses.yml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv("MITSYNC_WORKSPACE", str(ws))
    settings = Settings()
    settings._paths = Paths(workspace=ws, repo=ws / "_agent")

    entries = load_course_map(settings)

    assert [e["folder"] for e in entries] == [
        "Analytics Tools",
        "Analytics Edge",
        "Machine Learning",
        "Analytics Lab",
        "From Anaytics to Action",  # the student's own spelling; intentional
        "Optimization",
        "AI_Studio",
    ]
    assert [e["canvas_id"] for e in entries] == [38494, 38522, 38524, 38521, 38537, 38615, 40577]
    assert 17557 not in {e["canvas_id"] for e in entries}
    for entry in entries:
        assert entry["course_number"]
        assert entry["aliases"]
        assert all(isinstance(a, str) for a in entry["aliases"])


def test_shipped_settings_yml_declares_exclude_courses() -> None:
    raw = yaml.safe_load((REPO_ROOT / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert raw["canvas"]["exclude_courses"] == []
    settings = load_settings(REPO_ROOT / "config" / "settings.yml")
    assert settings.canvas.exclude_courses == []

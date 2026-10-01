"""readings.py: required syllabus readings become deadlines once they are on disk."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from mitsync.core.config import Settings
from mitsync.schedule import deadlines, readings
from tests.test_deadlines import _in, seeded  # noqa: F401 -- fixture

ML = "Machine Learning"


def _write(settings: Settings, entries: list[dict], course: str = ML) -> None:
    path = readings.readings_path(settings, course)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"syllabus": f"{course}/syllabus/syllabus.pdf", "readings": entries}
    path.write_text(json.dumps(doc))


def _reading(title: str, *, required: bool = True, file: str | None = None, days: float = 3):
    return {
        "title": title,
        "session": "Class 4",
        "due_at": _in(days),
        "required": required,
        "file": file,
    }


def _case(settings: Settings, name: str) -> str:
    rel = f"{ML}/case studies/{name}"
    path = settings.paths.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 case")
    return rel


def test_a_required_reading_on_disk_is_a_deadline(seeded: Settings) -> None:  # noqa: F811
    moderna = _case(seeded, "moderna_a.pdf")
    food = _case(seeded, "food.pdf")
    _write(
        seeded,
        [
            _reading("Moderna (A)", file=moderna),
            _reading("Food security", required=False, file=food),
            _reading("Accelerate!", file=None),  # not posted yet
        ],
    )

    report = deadlines.build_due(seeded)

    [row] = [i for i in report.items if i["type"] == "reading"]
    assert row == {
        **row,
        "course": ML,
        "title": "Read: Moderna (A)",
        "source": "syllabus",
        "status": "to_read",
        "submitted": False,
    }
    assert moderna in row["description"] and "Class 4" in row["description"]
    assert deadlines.in_window(row, datetime.now(UTC), 7)


def test_an_invalid_readings_file_is_a_warning_not_a_crash(seeded: Settings) -> None:  # noqa: F811
    _write(seeded, [{"title": "Moderna (A)", "required": "yes"}])
    report = deadlines.build_due(seeded)
    assert not [i for i in report.items if i["type"] == "reading"]
    assert any("readings.json" in w for w in report.warnings)
    assert report.items, "the Canvas deadlines are still there"


def test_check_lists_unmatched_cases_and_missing_files(seeded: Settings) -> None:  # noqa: F811
    named = _case(seeded, "netflix.pdf")
    _case(seeded, "tiers.pdf")
    (seeded.paths.workspace / ML / "case studies" / ".DS_Store").write_bytes(b"x")
    _write(
        seeded,
        [_reading("Netflix", file=named), _reading("Gone", file=f"{ML}/case studies/gone.pdf")],
    )

    found = readings.check_readings(seeded)

    by_code = {v["code"]: v["message"] for v in found}
    assert len(found) == 2
    assert by_code["case_unmatched"].startswith(f"{ML}/case studies/tiers.pdf ")
    assert "gone.pdf" in by_code["reading_file_missing"]

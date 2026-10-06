"""
# Readings

The cases and articles a syllabus assigns before a class, as deadlines.

## 1. What This Module Does

`load_readings` reads every `_kb/courses/<Course>/readings.json`, validates it
against `READINGS_SCHEMA` and returns its entries. `reading_items` turns each
**required** reading whose file is on disk into a deadline row (type
`reading`, source `syllabus`, due when the class starts), which `build_due`
merges with the Canvas assignments. `check_readings` is what `mitsync kb check`
reports: invalid files, missing files, and documents in a course's `case
studies/` folder that no reading names.

## 2. Why This Module Exists

A required case has no Canvas assignment. The only place that says "read
Moderna (A) before Class 4" is the syllabus, a PDF. Reading it is judgment,
so the agent does it once and writes the answer down here. From then on the
deadline is a fact like any other, and the morning brief lists it as homework
the day it is in the window, without the agent re-reading the syllabus.

## 3. How It Fits in the Architecture

Beside `deadlines` in `schedule/`, which imports it. It reads files on disk
only, never Canvas, and never writes: the agent writes `readings.json`.

## 4. Key Concepts

**Required and uploaded.** A reading becomes a deadline only when the syllabus
requires it (`required: true`) and its `file` exists in the course folder. An
optional reading, or one Canvas has not posted yet, stays out of `due`. The
agent sets `file` when the case is filed (the `mit-organize` skill).

**Invalid is reported, not fatal.** A bad `readings.json` becomes a warning in
`due` and an error in `kb check`. A deadline list that dies on one course's
notes would be worse than one missing that course's readings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mitsync.filing.course_map import existing_course_folders

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

__all__ = [
    "READINGS_FILE",
    "READINGS_SCHEMA",
    "check_readings",
    "load_readings",
    "reading_items",
    "readings_path",
]

READINGS_FILE = "readings.json"
CASE_FOLDER = "case studies"
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".doc", ".pptx", ".md", ".txt", ".html"}

READINGS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Readings a course's syllabus assigns (written by the agent)",
    "type": "object",
    "additionalProperties": False,
    "required": ["syllabus", "readings"],
    "properties": {
        "syllabus": {
            "type": "string",
            "description": "workspace-relative path of the syllabus this was read from",
        },
        "readings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "session", "due_at", "required", "file"],
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "session": {
                        "type": "string",
                        "maxLength": 40,
                        "description": "the class it is read for, e.g. 'Class 4'",
                    },
                    "due_at": {
                        "type": "string",
                        "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)$",
                        "description": "when that class starts, ISO 8601 with offset",
                    },
                    "required": {"type": "boolean"},
                    "file": {
                        "type": ["string", "null"],
                        "description": "workspace-relative path once it is filed, else null",
                    },
                },
            },
        },
    },
}


def readings_path(settings: Settings, course: str) -> Path:
    return settings.paths.kb_courses / course / READINGS_FILE


def load_readings(settings: Settings) -> tuple[list[dict[str, Any]], list[str]]:
    """Every valid course's readings, each with its `course`, and the errors."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(READINGS_SCHEMA)
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for course in existing_course_folders(settings):
        path = readings_path(settings, course)
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{path}: not valid JSON ({exc})")
            continue
        problems = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
        if problems:
            where = "/".join(str(p) for p in problems[0].absolute_path) or "<root>"
            errors.append(f"{path}: at {where}: {problems[0].message}")
            continue
        out.extend({**r, "course": course} for r in doc["readings"])
    return out, errors


def reading_items(settings: Settings, warnings: list[str]) -> list[dict[str, Any]]:
    """Required readings whose file is in the course folder, as deadline rows."""
    from mitsync.schedule.deadlines import _item

    readings, errors = load_readings(settings)
    warnings.extend(errors)
    ws = settings.paths.workspace
    return [
        _item(
            r["course"],
            f"Read: {r['title']}",
            r["due_at"],
            "reading",
            None,
            "syllabus",
            False,
            status="to_read",
            description=f"Required reading for {r['session']} (syllabus). File: {r['file']}",
        )
        for r in readings
        if r["required"] and r["file"] and (ws / r["file"]).is_file()
    ]


def check_readings(settings: Settings) -> list[dict[str, str]]:
    """What the agent still owes the readings files, as `{code, course, message}`.

    - `readings_invalid`: a `readings.json` that does not validate.
    - `reading_file_missing`: a reading names a `file` that is not on disk.
    - `case_unmatched`: a document in `<Course>/case studies/` no reading names.
      Read the syllabus and add it (`required: false` if it is optional or not
      in the syllabus), so it is never asked about again.
    """
    readings, errors = load_readings(settings)
    out = [{"code": "readings_invalid", "course": "", "message": e} for e in errors]
    ws = settings.paths.workspace
    named = {r["file"] for r in readings if r["file"]}
    for r in readings:
        if r["file"] and not (ws / r["file"]).is_file():
            out.append(
                {
                    "code": "reading_file_missing",
                    "course": r["course"],
                    "message": f"{r['title']!r} names {r['file']}, which is not on disk",
                }
            )
    for course in existing_course_folders(settings):
        folder = ws / course / CASE_FOLDER
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            rel = f"{course}/{CASE_FOLDER}/{path.name}"
            if path.is_file() and path.suffix.lower() in DOCUMENT_SUFFIXES and rel not in named:
                out.append(
                    {
                        "code": "case_unmatched",
                        "course": course,
                        "message": f"{rel} is in no reading of "
                        f"_kb/courses/{course}/{READINGS_FILE}: find it in the syllabus",
                    }
                )
    return out

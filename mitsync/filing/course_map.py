"""
# Course Map

Which Canvas course is which of the student's own folders, and which
workspace folders are courses at all.

## 1. What This Module Does

Reads `config/courses.yml` -- the Canvas course id -> workspace folder mapping
(`load_course_map`, `folder_for_canvas_id`) -- and answers which of those
folders exist on disk (`existing_course_folders`). It also owns the shared
ignore test for workspace paths (`is_ignored`) and knows where `config/naming.md` lives.

## 2. Why This Module Exists

Canvas names a course "Fall 2026 - 15.095 Machine Learning Under a Modern
Optimization Lens"; the student calls the folder "Machine Learning". Every other
feature -- filing, deadlines, calendar tagging, extraction -- needs that
translation, and most of them need nothing else. Keeping it here means
`deadlines` and `calendar_read` can read fifteen lines of YAML without importing
the validate / apply / undo machinery in :mod:`mitsync.filing.organize`.

## 3. How It Fits in the Architecture

The bottom of the filing stack: `organize` imports this module, never the other
way round. `calendar_read` imports only `load_course_map`; `deadlines` and
`extract` also use `existing_course_folders`, for `mitsync work` and for the
extraction source roots.
`config/courses.yml` is written and maintained by hand; nothing in mitsync
writes it.

## 4. Key Concepts

**A course folder is one `courses.yml` names.** The workspace root is shared
with other tools -- OpenClaw keeps `memory/`, `AGENTS.md` and friends there --
so "any top-level directory" is not a course. `existing_course_folders` returns
only the mapped folders that exist on disk, live directly in the workspace (not
a symlink into `_agent/` or elsewhere), and are not ignored. Everything that
reads the student's folders -- `organize`'s destination and existing-file
checks, `work`, extraction -- goes through it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from mitsync.core.errors import ConfigError
from mitsync.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

log = get_logger(__name__)

__all__ = [
    "courses_file",
    "existing_course_folders",
    "folder_for_canvas_id",
    "load_course_map",
]

#: `_`, `-` and `.` are separators everywhere a bucket name
#: has to be read out of a filename.
SEPARATORS_RX = re.compile(r"[._\-]+")


def is_ignored(settings: Settings, relpath: str) -> bool:
    """True if a workspace-relative path (file or directory) is excluded.

    Directory globs such as ``AI_Studio/nandatown/**`` only match *contents*, so
    a directory is also probed with a trailing slash. This is what keeps the
    9,400-file `nandatown` repo out of every walk, plan and report.
    """
    rel = str(relpath).strip("/")
    if not rel:
        return False
    return settings.should_ignore(rel) or settings.should_ignore(rel + "/")


def naming_rules_path(settings: Settings) -> Path:
    """Where ``config/naming.md`` -- the filing policy the agent reads -- lives."""
    return settings.paths.config_dir / "naming.md"


# --------------------------------------------------------------------------
# course map
# --------------------------------------------------------------------------
def courses_file(settings: Settings) -> Path:
    return settings.paths.config_dir / "courses.yml"


def load_course_map(settings: Settings) -> list[dict[str, Any]]:
    """Entries from ``config/courses.yml``; ``[]`` when unset or absent."""
    path = courses_file(settings)
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    entries = raw.get("courses") or []
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: `courses` must be a list")
    return [dict(e) for e in entries if isinstance(e, dict)]


def folder_for_canvas_id(settings: Settings, canvas_id: Any) -> str | None:
    """The student's folder for a Canvas course id, per ``config/courses.yml``."""
    if canvas_id in (None, ""):
        return None
    for entry in load_course_map(settings):
        if str(entry.get("canvas_id")) == str(canvas_id):
            folder = entry.get("folder")
            return str(folder) if folder else None
    return None


def existing_course_folders(settings: Settings) -> list[str]:
    """Folders named in ``config/courses.yml`` that really exist in the workspace.

    Only mapped folders count: the workspace root is shared with OpenClaw
    (`memory/`, `AGENTS.md`, `SOUL.md`, ...), so a top-level directory is not a
    course just because it is there. Unmapped, missing, ignored and reserved
    (`_`/`.`) names are skipped, as is anything that resolves outside the
    workspace root -- `<workspace>/skills` is a symlink to `_agent/skills`, and
    machinery is not a course.
    """
    ws = settings.paths.workspace
    if not ws.exists():
        return []
    ws_resolved = ws.resolve()
    out: list[str] = []
    for entry in load_course_map(settings):
        name = str(entry.get("folder") or "").strip()
        if not name or name.startswith((".", "_")) or "/" in name or name in out:
            continue
        child = ws / name
        if not child.is_dir() or is_ignored(settings, name):
            continue
        if child.resolve().parent != ws_resolved:
            continue
        out.append(name)
    return sorted(out)

"""
# Organize

File the Canvas mirror into the student's own course folders -- from a plan
the driving agent wrote, applied only on confirmation, and always undoable.

## 1. What This Module Does

`unfiled`
    List every mirrored file the manifest has not filed yet, with the context
    an agent needs to decide where it goes (course folder, Canvas module and
    folder), plus the allowed buckets and the path to `config/naming.md`.
    **Deciding the destination is the agent's job, not this module's.**
`validate_plan`
    Check an agent-written plan -- `{"placements": [{"file_id", "destination",
    "reason"}], "skips": [{"file_id", "reason"}]}` -- against its JSON schema
    and against the structural filing rules below, splitting it into accepted
    placements, skips and rejections, each rejection with its reason.
`apply_plan` / `undo`
    Execute the accepted placements after confirmation and reverse them.
    Canvas-mirrored files are *linked or copied* into the curated folder;
    `_canvas/` stays the source of truth and is never emptied. Only
    pre-existing student files are really moved, and only those need -- and
    get -- an undo entry that restores them byte-for-byte.

## 2. Why This Module Exists

The student's course folders are their own. A tool that reorganises them on a
model's say-so, with no check and no way back, is not one anybody should run.
Hence the validation of every placement, the refusal to touch a pre-existing
file without `--include-existing`, the confirmation before anything is
applied, and the undo log that refuses to reverse anything whose bytes changed
in the meantime.

## 3. How It Fits in the Architecture

Sits above `manifest` (which says what is mirrored and not yet filed) and
`course_map` (which says which Canvas course is which folder), and below
`kb`, which reuses `BUCKETS` and `classify_bucket` for its display order.
`organize` imports `course_map`, never the reverse.

## 4. Key Concepts

**The filing policy is prose; only its structure is code.** `config/naming.md`
says how material is filed -- which bucket, which per-item folder name, when to
rename -- and the agent reads it itself. This module enforces only the
*shape* that prose promises, because a shape can be checked and a judgment
cannot: a destination lies inside a course folder that `config/courses.yml`
maps, its second component is one of `FILING_BUCKETS`, the `PER_ITEM_BUCKETS`
(`assignments/`, `recitations/`) hold only per-item folders
(`<course>/<bucket>/<item>/<file>`), and every other bucket is flat
(`<course>/<bucket>/<file>`). If naming.md's structure ever changes, these
constants must change with it.

**Destinations are untrusted input.** They come from a language model.
`validate_destination` rejects absolute paths, `..` traversal, anything
outside the workspace, the reserved `_canvas`/`_agent`/`_kb` trees, ignored
paths, and directory-only destinations -- at validation time *and* again at
apply time, because the disk can change between the two.

**A name collision never overwrites the student's work.** An identical file
already at the destination is a skip that still records the file as filed. A
different one rejects the placement, with one exception: mitsync's own filed
copy of another Canvas file, byte-for-byte as filed, is replaced (a post-class
deck taking the pre-class deck's name). The displaced file is marked skipped,
and undo puts it back.

**Skips are decisions, not gaps.** A skipped file (`skip_reason` in the
manifest) is one naming.md says the student does not want. `unfiled` stops
listing it, and placing it later clears the skip. Skipping a file that is
already filed removes its unchanged filed copy, and needs `--include-existing`.

**Renaming a filed copy is a move of the student's folder.** Placing an
already-filed Canvas file again by its `file_id` moves its filed copy to the
new name and updates the manifest. Like any change to what is already in the
course folders, it needs `--include-existing`, and it is refused if the copy
changed since it was filed. An `existing:` placement may not name a filed
copy, or the manifest would lose track of it.

**Buckets.** `BUCKETS` is the wider display vocabulary the knowledge base
groups by, including `data/` for course folders that predate naming.md's
"no course-wide data folder" rule. `classify_bucket` reads a bucket out of a
path for that display; it is never used to decide a filing destination.

**Why exceptions are caught here.** Four, each at a real boundary:

1. `ValueError` from `validate_destination` -- this is untrusted agent output
   being validated. A bad destination is recorded as a rejected placement with
   the reason, and the run continues; one hallucinated path must not cost the
   other forty placements.
2. `jsonschema.ValidationError` -- the plan file as a whole is malformed. It
   becomes a `MitsyncError` naming the file and the failing JSON path, and
   nothing is applied.
3. `OSError` around an individual file operation -- a hardlink across
   filesystems falls back to a copy, and a failed move or unlink is recorded
   per file so the undo log still describes everything that *did* happen. An
   apply that aborted midway with no log would be the worst possible outcome.
4. `OSError` in `_prune_empty`, narrowed to `ENOTEMPTY`/`EEXIST` -- on macOS
   Finder can write a `.DS_Store` between the emptiness check and the `rmdir`.
   Losing that race means the directory is no longer ours to remove, so the
   climb stops. Every other `OSError` raises: a directory this tool can
   neither read nor remove inside its own workspace is a state worth failing
   on, not one to pass over in silence.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import jsonschema
from rich.console import Console
from rich.table import Table

from mitsync.core.clock import now_iso
from mitsync.core.config import read_json, read_meta
from mitsync.core.errors import MitsyncError
from mitsync.core.logging import get_logger
from mitsync.core.paths import unique_path
from mitsync.filing.course_map import (
    SEPARATORS_RX,
    existing_course_folders,
    folder_for_canvas_id,
    is_ignored,
    naming_rules_path,
)

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

log = get_logger(__name__)
console = Console()

__all__ = [
    "EXISTING_PREFIX",
    "FILING_BUCKETS",
    "PER_ITEM_BUCKETS",
    "PLAN_SCHEMA",
    "ApplyReport",
    "Plan",
    "PlanEntry",
    "UndoReport",
    "apply_plan",
    "undo",
    "unfiled",
    "validate_plan",
]

#: Every bucket the knowledge base may group a file under, in display order.
#: Wider than `FILING_BUCKETS`: folders filed before naming.md dropped `data/`
#: still exist and must still be shown.
BUCKETS = (
    "lectures",
    "case studies",
    "recitations",
    "assignments",
    "data",
    "syllabus",
    "notes",
    "other",
)

#: The subfolders a *new* placement may target (config/naming.md §2).
FILING_BUCKETS = (
    "lectures",
    "case studies",
    "recitations",
    "assignments",
    "syllabus",
    "notes",
    "other",
)

#: Buckets that hold only per-item folders, never loose files (naming.md §2).
PER_ITEM_BUCKETS = ("assignments", "recitations")

#: Top-level names that are mitsync's own, never a filing destination.
RESERVED_TOP_LEVEL = ("_canvas", "_agent", "_kb")

#: `file_id` prefix for a file already in a course folder (not from the mirror).
EXISTING_PREFIX = "existing:"

#: What an agent-written plan must look like. Structural rules beyond shape are
#: checked per placement by `validate_plan`.
PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["placements"],
    "additionalProperties": False,
    "properties": {
        "placements": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file_id", "destination", "reason"],
                "additionalProperties": False,
                "properties": {
                    "file_id": {"type": "string", "minLength": 1},
                    "destination": {"type": "string", "minLength": 1},
                    "reason": {"type": "string"},
                },
            },
        },
        "skips": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file_id", "reason"],
                "additionalProperties": False,
                "properties": {
                    "file_id": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

#: Bucket classification for *display*, ordered: first match wins. Patterns run
#: against a normalised string where `_`, `-` and `.` become spaces, so real
#: filenames like `15_095_hw1.pdf` and `Lec03_2026.pdf` tokenise the way a
#: reader would expect rather than hiding the signal inside one long word.
_BUCKET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "syllabus",
        re.compile(r"\bsyllab|\bcourse info|\blogistics?\b|\bgrading\b|\bschedule\b", re.I),
    ),
    (
        "recitations",
        re.compile(r"\brecit\w*|\brec\b|\brec\s?\d|\bsection\b|\bsection\s?\d|\btutorial", re.I),
    ),
    (
        "assignments",
        re.compile(
            r"\bhw\b|\bhw\s?\d|homework|assign\w*|deliv\w*|\bpset|problem\s?set"
            r"|\bps\s?\d|\bexam\b|midterm|\bquiz|\bproject\b|solution",
            re.I,
        ),
    ),
    (
        "lectures",
        re.compile(
            r"\blec\b|\blec\s?\d|lecture|\bslides?\b|\bunit\b|\bunit\s?\d|\bmodule\b"
            r"|\bsession\b|\bsession\s?\d|\bclass\s?\d|\bweek\s?\d",
            re.I,
        ),
    ),
    ("notes", re.compile(r"\bnotes?\b", re.I)),
)

#: Extensions that display as `data/` regardless of what they are called.
_DATA_EXTS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".parquet"}


def classify_bucket(filename: str, *grouping: str | None) -> str:
    """The display bucket for one file, for the knowledge base's grouping.

    `grouping` is the context the file was found in -- the student's own
    subfolders -- in priority order. Grouping always wins over the filename (a
    file inside "Recitation 2/" is a recitation even when it is called
    `walkthrough.pdf`). Only when no grouping text matches does the data
    extension, and then the filename itself, decide. Never used to choose a
    filing destination: that is the agent's call, made from naming.md.
    """
    for text in grouping:
        if not text:
            continue
        for bucket, rx in _BUCKET_PATTERNS:
            if rx.search(SEPARATORS_RX.sub(" ", text)):
                return bucket
    if PurePosixPath(filename).suffix.lower() in _DATA_EXTS:
        return "data"
    stem = SEPARATORS_RX.sub(" ", PurePosixPath(filename).stem)
    for bucket, rx in _BUCKET_PATTERNS:
        if rx.search(stem):
            return bucket
    return "other"


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------
@dataclass
class PlanEntry:
    """One accepted placement. ``uuid`` is empty for pre-existing files.

    ``replaces`` is the uuid of the Canvas file whose filed copy this placement
    displaces (a post-class deck taking over the pre-class deck's name).
    """

    file_id: str
    uuid: str
    source: str
    destination: str
    reason: str
    kind: str = "canvas"  # canvas | refile | existing
    replaces: str = ""


@dataclass
class Plan:
    """An agent-written plan after validation: what will run and what will not."""

    plan_id: str
    entries: list[PlanEntry] = field(default_factory=list)
    skips: list[dict[str, str]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    path: Path | None = None


@dataclass
class ApplyReport:
    plan_id: str
    link_mode: str = "hardlink"
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    confirmed: bool = False
    undo_log: Path | None = None


@dataclass
class UndoReport:
    log_id: str
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# destination guardrails
# --------------------------------------------------------------------------
def validate_destination(settings: Settings, destination: str) -> str:
    """Return the cleaned workspace-relative destination, or raise ``ValueError``.

    Rejects absolute paths, `..` traversal, anything outside the workspace, the
    reserved `_canvas`/`_agent`/`_kb` trees, ignored paths, and directory-only
    destinations.
    """
    raw = str(destination or "").strip()
    if not raw:
        raise ValueError("destination is empty")
    if raw.startswith("/") or raw.startswith("~") or PurePosixPath(raw).is_absolute():
        raise ValueError("destination must be workspace-relative, not absolute")
    if "\x00" in raw:
        raise ValueError("destination contains a NUL byte")
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError("destination must be workspace-relative, not absolute")
    parts = [p for p in PurePosixPath(raw.replace("\\", "/")).parts if p not in (".",)]
    if any(p == ".." for p in parts):
        raise ValueError("destination may not traverse upward with '..'")
    if len(parts) < 2:
        raise ValueError("destination must be <course folder>/<subpath>/<filename>")
    if parts[0] in RESERVED_TOP_LEVEL:
        raise ValueError(f"destination may not target the reserved '{parts[0]}/' tree")
    rel = PurePosixPath(*parts).as_posix()
    if is_ignored(settings, rel):
        raise ValueError(f"destination '{rel}' is excluded by ignore_globs")
    target = settings.paths.workspace / rel
    if not settings.paths.is_inside_workspace(target.parent):
        raise ValueError("destination escapes the workspace")
    if target.is_dir():
        raise ValueError("destination is an existing directory, not a file path")
    return rel


def validate_structure(settings: Settings, rel: str, courses: list[str] | None = None) -> None:
    """Raise ``ValueError`` unless ``rel`` has the shape naming.md promises.

    ``<course>/<bucket>/<file>``, or ``<course>/<bucket>/<item>/<file>`` for a
    per-item bucket, where the course is a folder `courses.yml` maps and that
    exists on disk.
    """
    parts = PurePosixPath(rel).parts
    known = courses if courses is not None else existing_course_folders(settings)
    if parts[0] not in known:
        raise ValueError(
            f"'{parts[0]}' is not a course folder listed in config/courses.yml "
            f"(known: {', '.join(known) or 'none'})"
        )
    if len(parts) < 3:
        raise ValueError("destination must be <course>/<bucket>/.../<filename>")
    bucket = parts[1]
    if bucket not in FILING_BUCKETS:
        raise ValueError(f"'{bucket}/' is not an allowed bucket ({', '.join(FILING_BUCKETS)})")
    if bucket in PER_ITEM_BUCKETS:
        if len(parts) == 3:
            raise ValueError(
                f"'{bucket}/' holds only per-item folders: use "
                f"{parts[0]}/{bucket}/<item>/{parts[2]}"
            )
        if len(parts) > 4:
            raise ValueError(f"never nest below the per-item folder in '{bucket}/'")
    elif len(parts) > 3:
        raise ValueError(f"'{bucket}/' is flat: no subfolders (got {'/'.join(parts[2:-1])}/)")


# --------------------------------------------------------------------------
# what is waiting to be filed
# --------------------------------------------------------------------------
def _canvas_candidates(settings: Settings) -> dict[str, dict[str, Any]]:
    """file_id -> every mirrored file in the manifest, filed or not."""
    db = settings.paths.manifest_db
    if not db.exists():
        return {}
    from mitsync.canvas.manifest import Manifest

    out: dict[str, dict[str, Any]] = {}
    with Manifest(db) as man:
        for rec in man.list_files():
            if not rec.mirror_path or is_ignored(settings, rec.mirror_path):
                continue
            file_id = str(rec.canvas_id or rec.uuid)
            out[file_id] = {
                "file_id": file_id,
                "uuid": rec.uuid,
                "source": rec.mirror_path,
                "sha256": rec.sha256,
                "display_name": rec.display_name or rec.filename,
                "course": folder_for_canvas_id(settings, rec.course_canvas_id),
                "mirror_course": rec.course_folder,
                "canvas_folder": rec.canvas_folder,
                "module_name": rec.module_name,
                "module_position": rec.module_position,
                "content_type": rec.content_type,
                "size": rec.size,
                "filed_path": rec.filed_path,
                "skip_reason": rec.skip_reason,
            }
    return out


def _module_items(settings: Settings) -> list[dict[str, Any]]:
    """Every module item in the mirror, with the course folder and the nearest
    `SubHeader` above it -- the text that says "PostClass" or "Lecture 5" when
    the filename does not."""
    out: list[dict[str, Any]] = []
    for meta in sorted(settings.paths.canvas_mirror.glob("*/_meta/modules.json")):
        course = folder_for_canvas_id(settings, read_json(meta)["course_canvas_id"])
        for module in read_meta(meta):
            subheader = None
            for item in module.get("items") or []:
                if item.get("type") == "SubHeader":
                    subheader = item.get("title")
                    continue
                out.append({"course": course, "module": module, "subheader": subheader, **item})
    return out


def unfiled(settings: Settings) -> dict[str, Any]:
    """Every mirrored file neither filed nor skipped, plus the rules the agent
    files by, and the case links in modules that `sync` has not downloaded
    (yet, or at all: its report says why).

    Each file carries its module item's title and subheader: the course's own
    label ("PostClass CART Regression Slides") often says more than the
    filename. The agent reads `naming_rules` itself and writes a plan matching
    `plan_schema`; `organize apply --plan` validates and applies it.
    """
    items = _module_items(settings)
    by_file_id = {
        str(i["content_id"]) if i["type"] == "File" else f"link-{i['id']}": i for i in items
    }
    candidates = _canvas_candidates(settings)
    files = []
    for c in candidates.values():
        if c["filed_path"] or c["skip_reason"]:
            continue
        item = by_file_id.get(c["file_id"], {})
        files.append(
            {
                k: v
                for k, v in c.items()
                if k not in ("uuid", "source", "sha256", "filed_path", "skip_reason")
            }
            | {
                "mirror_path": c["source"],
                "module_item_title": item.get("title"),
                "module_subheader": item.get("subheader"),
            }
        )
    files.sort(key=lambda f: (f["course"] or "", f["mirror_path"]))
    links = [
        {
            "course": i["course"],
            "title": i["title"],
            "module_name": i["module"].get("name"),
            "canvas_url": i.get("html_url"),
        }
        for i in items
        if i["type"] == "ExternalTool" and i["course"] and f"link-{i['id']}" not in candidates
    ]
    return {
        "naming_rules": str(naming_rules_path(settings)),
        "course_folders": existing_course_folders(settings),
        "buckets": list(FILING_BUCKETS),
        "per_item_buckets": list(PER_ITEM_BUCKETS),
        "existing_file_id_prefix": EXISTING_PREFIX,
        "plans_dir": str(settings.paths.plans_dir),
        "plan_schema": PLAN_SCHEMA,
        "files": files,
        "links": links,
    }


# --------------------------------------------------------------------------
# validate an agent-written plan
# --------------------------------------------------------------------------
def load_plan_document(path: Path | str) -> dict[str, Any]:
    """Read a plan file and check it against `PLAN_SCHEMA`, naming the failing path."""
    path = Path(path)
    if not path.exists():
        raise MitsyncError(f"plan file not found: {path}")
    doc = read_json(path)
    try:
        jsonschema.validate(doc, PLAN_SCHEMA)
    except jsonschema.ValidationError as exc:
        where = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in exc.path)
        raise MitsyncError(
            f"{path} does not match the plan schema at {where}: {exc.message}"
        ) from exc
    return doc


def _existing_source(
    settings: Settings, rel: str, courses: list[str], candidates: dict[str, dict[str, Any]]
) -> str:
    """Validate the source of an `existing:` placement; returns it cleaned."""
    raw = rel.strip()
    parts = [p for p in PurePosixPath(raw).parts if p != "."]
    if not parts or raw.startswith(("/", "~")) or ".." in parts:
        raise ValueError("existing source must be a workspace-relative path")
    clean = PurePosixPath(*parts).as_posix()
    if is_ignored(settings, clean):
        raise ValueError(f"existing source '{clean}' is excluded by ignore_globs")
    if parts[0] not in courses:
        raise ValueError(f"existing source '{clean}' is not inside a mapped course folder")
    path = settings.paths.workspace / clean
    if not path.is_file() or not settings.paths.is_inside_workspace(path):
        raise ValueError(f"existing source '{clean}' does not exist")
    for candidate in candidates.values():
        if candidate["filed_path"] == clean:
            raise ValueError(
                f"'{clean}' is mitsync's filed copy of file_id {candidate['file_id']}: "
                "place it by that file_id so the manifest follows it"
            )
    return clean


def validate_plan(
    settings: Settings, plan_path: Path | str, *, include_existing: bool = False
) -> Plan:
    """Split an agent-written plan into accepted entries, skips and rejections.

    Touches nothing on disk. Each rejection carries the placement's
    ``file_id``, its ``destination`` (``-`` for a skip) and the ``reason``.

    A file already filed may be placed again -- a rename -- only with
    ``include_existing``, and only while its filed copy is byte-for-byte what
    was filed. A destination holding another file is rejected unless that file
    is mitsync's own unchanged filed copy of a Canvas file, which the placement
    then replaces (`PlanEntry.replaces`).
    """
    path = Path(plan_path)
    doc = load_plan_document(path)
    the_plan = Plan(plan_id=path.stem, path=path)
    courses = existing_course_folders(settings)
    candidates = _canvas_candidates(settings)
    by_filed_path = {c["filed_path"]: c for c in candidates.values() if c["filed_path"]}
    ws = settings.paths.workspace

    seen_ids: set[str] = set()
    seen_dests: set[str] = set()

    def reject(entry: dict[str, Any], reason: str) -> None:
        the_plan.rejected.append(
            {
                "file_id": entry["file_id"],
                "destination": entry.get("destination", "-"),
                "reason": reason,
            }
        )

    for placement in doc["placements"]:
        file_id = placement["file_id"]
        if file_id in seen_ids:
            reject(placement, "duplicate placement for this file_id")
            continue
        seen_ids.add(file_id)

        try:
            if file_id.startswith(EXISTING_PREFIX):
                if not include_existing:
                    raise ValueError(
                        "pre-existing student file: refused without --include-existing"
                    )
                source, uuid, kind = (
                    _existing_source(
                        settings, file_id[len(EXISTING_PREFIX) :], courses, candidates
                    ),
                    "",
                    "existing",
                )
            else:
                candidate = candidates.get(file_id)
                if candidate is None:
                    raise ValueError("no mirrored file with this file_id in the manifest")
                if candidate["filed_path"]:
                    filed = candidate["filed_path"]
                    if not include_existing:
                        raise ValueError(
                            f"already filed at {filed}; moving it needs --include-existing"
                        )
                    if not (ws / filed).is_file():
                        raise ValueError(f"the filed copy at {filed} is gone")
                    if sha256_file(ws / filed) != candidate["sha256"]:
                        raise ValueError(f"the filed copy at {filed} changed since it was filed")
                    source, uuid, kind = filed, candidate["uuid"], "refile"
                else:
                    if not (ws / candidate["source"]).is_file():
                        raise ValueError(f"source missing from the mirror: {candidate['source']}")
                    source, uuid, kind = candidate["source"], candidate["uuid"], "canvas"

            dest = validate_destination(settings, placement["destination"])
            validate_structure(settings, dest, courses)
            if dest == source:
                raise ValueError("already at its destination")
            if dest in seen_dests:
                raise ValueError("another placement in this plan targets the same destination")
            target = ws / dest
            replaces = ""
            if target.exists() and sha256_file(target) != sha256_file(ws / source):
                occupant = by_filed_path.get(dest)
                if occupant is None or kind == "existing":
                    raise ValueError("a different file already exists at the destination")
                if sha256_file(target) != occupant["sha256"]:
                    raise ValueError(
                        f"the filed copy of {occupant['file_id']} at the destination "
                        "changed since it was filed"
                    )
                replaces = occupant["uuid"]
        except ValueError as exc:
            reject(placement, str(exc))
            continue

        seen_dests.add(dest)
        the_plan.entries.append(
            PlanEntry(
                file_id=file_id,
                uuid=uuid,
                source=source,
                destination=dest,
                reason=placement["reason"],
                kind=kind,
                replaces=replaces,
            )
        )

    for skip in doc.get("skips", []):
        file_id = skip["file_id"]
        candidate = candidates.get(file_id)
        if file_id in seen_ids:
            reject(skip, "this file_id is already placed or skipped in this plan")
        elif candidate is None:
            reject(skip, "no mirrored file with this file_id in the manifest")
        elif candidate["filed_path"] and not include_existing:
            reject(
                skip,
                f"already filed at {candidate['filed_path']}; "
                "removing a filed copy needs --include-existing",
            )
        elif candidate["filed_path"] and (
            not (ws / candidate["filed_path"]).is_file()
            or sha256_file(ws / candidate["filed_path"]) != candidate["sha256"]
        ):
            reject(skip, f"the filed copy at {candidate['filed_path']} is gone or changed")
        else:
            the_plan.skips.append(
                {
                    "file_id": file_id,
                    "uuid": candidate["uuid"],
                    "reason": skip["reason"],
                    "source": candidate["source"],
                    "filed_path": candidate["filed_path"] or "",
                }
            )
        seen_ids.add(file_id)
    return the_plan


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------
def _newest(directory: Path, pattern: str) -> Path | None:
    candidates = sorted(directory.glob(pattern))
    return candidates[-1] if candidates else None


def _confirm(the_plan: Plan, *, yes: bool) -> bool:
    if yes:
        return True
    moves = sum(1 for e in the_plan.entries if e.kind == "existing")
    renames = sum(1 for e in the_plan.entries if e.kind == "refile")
    replaced = sum(1 for e in the_plan.entries if e.replaces)
    what = f"{len(the_plan.entries)} placement(s)"
    if moves:
        what += f", {moves} of them MOVING a pre-existing file"
    if renames:
        what += f", {renames} of them moving a filed copy"
    if replaced:
        what += f", {replaced} of them replacing a filed copy"
    if the_plan.skips:
        removed = sum(1 for s in the_plan.skips if s["filed_path"])
        what += f", and {len(the_plan.skips)} skip(s) ({removed} removing a filed copy)"
    if not sys.stdin.isatty():
        console.print(
            f"[yellow]{what}. Refusing without --yes (no terminal to confirm on).[/yellow]"
        )
        return False
    answer = input(f"Apply {what}? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def _place(source: Path, dest: Path, mode: str) -> str:
    """Materialise ``dest`` from ``source``; returns the mode actually used."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "symlink":
        dest.symlink_to(source)
        return "symlink"
    if mode == "hardlink":
        try:
            os.link(source, dest)
            return "hardlink"
        except OSError as exc:  # different filesystem, or a FS with no hard links
            log.info("hardlink %s -> %s failed (%s); copying instead", source, dest, exc)
    shutil.copy2(source, dest)
    return "copy"


def apply_plan(
    settings: Settings,
    plan_path: Path | str,
    *,
    yes: bool = False,
    include_existing: bool = False,
) -> ApplyReport:
    """Validate an agent-written plan and execute what passes, writing an undo log.

    Rejected placements are reported and never applied. Nothing at all is
    applied without confirmation (interactive, or ``yes=True``).
    """
    the_plan = validate_plan(settings, plan_path, include_existing=include_existing)
    mode = settings.organize.link_mode
    report = ApplyReport(plan_id=the_plan.plan_id, link_mode=mode, rejected=the_plan.rejected)
    ws = settings.paths.workspace

    if not the_plan.entries and not the_plan.skips:
        report.confirmed = True  # nothing to confirm
        _print_report(report)
        return report
    report.confirmed = _confirm(the_plan, yes=yes)
    if not report.confirmed:
        for entry in the_plan.entries:
            report.skipped.append({**asdict(entry), "status": "not confirmed"})
        _print_report(report)
        return report

    operations: list[dict[str, Any]] = []
    # (uuid, filed_path, skip_reason) per Canvas file whose filing state changed.
    filing: list[tuple[str, str | None, str | None]] = []
    occupants = {c["uuid"]: c for c in _canvas_candidates(settings).values()}

    for entry in the_plan.entries:
        source = ws / entry.source
        if not source.exists():
            report.errors.append({"file_id": entry.file_id, "error": f"missing source {source}"})
            continue
        try:
            dest_rel = validate_destination(settings, entry.destination)
        except ValueError as exc:
            report.errors.append({"file_id": entry.file_id, "error": str(exc)})
            continue
        dest = ws / dest_rel

        if dest.exists():
            if sha256_file(dest) == sha256_file(source):
                report.skipped.append({**asdict(entry), "status": "identical file already there"})
                if entry.uuid:
                    filing.append((entry.uuid, dest_rel, None))
                continue
            occupant = occupants.get(entry.replaces)
            if occupant is None or sha256_file(dest) != occupant["sha256"]:
                report.errors.append(
                    {
                        "file_id": entry.file_id,
                        "error": "a different file appeared at the destination",
                    }
                )
                continue
            # The pre-class deck's filed copy, unchanged: its bytes stay in the mirror.
            dest.unlink()
            operations.append(
                {
                    "file_id": occupant["file_id"],
                    "uuid": occupant["uuid"],
                    "kind": "canvas",
                    "mode": "displace",
                    "source": occupant["source"],
                    "destination": dest_rel,
                }
            )
            filing.append((occupant["uuid"], None, f"replaced by {entry.file_id} at {dest_rel}"))

        src_sha = sha256_file(source)
        try:
            if entry.kind in ("existing", "refile"):
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))
                used = "move" if entry.kind == "existing" else "refile"
            else:
                used = _place(source, dest, mode)
        except OSError as exc:
            report.errors.append(
                {"file_id": entry.file_id, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue

        operations.append(
            {
                "file_id": entry.file_id,
                "uuid": entry.uuid,
                "kind": entry.kind,
                "mode": used,
                "source": entry.source,
                "destination": dest_rel,
                "source_sha256": src_sha,
                "destination_sha256": src_sha if used != "symlink" else None,
            }
        )
        report.applied.append({**asdict(entry), "destination": dest_rel, "mode": used})
        if entry.uuid:
            filing.append((entry.uuid, dest_rel, None))
        if entry.kind == "refile":
            _prune_empty(source.parent, ws)

    for skip in the_plan.skips:
        if skip["filed_path"]:
            # A filed copy the rules no longer want: its bytes stay in the mirror.
            (ws / skip["filed_path"]).unlink()
            _prune_empty((ws / skip["filed_path"]).parent, ws)
            operations.append(
                {
                    "file_id": skip["file_id"],
                    "uuid": skip["uuid"],
                    "kind": "canvas",
                    "mode": "displace",
                    "source": skip["source"],
                    "destination": skip["filed_path"],
                }
            )
        operations.append({**skip, "kind": "canvas", "mode": "skip"})
        filing.append((skip["uuid"], None, skip["reason"]))

    report.undo_log = _write_undo_log(settings, the_plan, mode, operations)
    _record_filing(settings, filing)
    _print_report(report)
    if the_plan.skips:
        console.print(f"skipped on purpose: {len(the_plan.skips)} file(s)")
    if report.undo_log is not None:
        console.print(f"undo log: [bold]{report.undo_log}[/bold]")
    return report


def _record_filing(settings: Settings, filing: list[tuple[str, str | None, str | None]]) -> None:
    if not filing:
        return
    from mitsync.canvas.manifest import Manifest

    with Manifest(settings.paths.manifest_db) as man:
        for uuid, filed_path, skip_reason in filing:
            man.set_filed_path(uuid, filed_path)
            man.set_skip_reason(uuid, skip_reason)


def _write_undo_log(
    settings: Settings, the_plan: Plan, mode: str, operations: list[dict[str, Any]]
) -> Path:
    path = unique_path(settings.paths.undo_dir / f"undo-{the_plan.plan_id}.json")
    doc = {
        "log_id": path.stem,
        "plan_id": the_plan.plan_id,
        "plan_path": str(the_plan.path) if the_plan.path else None,
        "applied_at": now_iso(),
        "link_mode": mode,
        "operations": operations,
        "undone_at": None,
    }
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def _print_report(report: ApplyReport) -> None:
    """The table, then one unwrapped line per rejection so each reason reads whole."""
    console.print(_apply_table(report))
    for row in report.rejected:
        # markup off: a destination is agent-written text and may contain "[...]".
        console.print(
            f"rejected {row['file_id']} -> {row['destination']}: {row['reason']}",
            soft_wrap=True,
            markup=False,
            highlight=False,
        )


def _apply_table(report: ApplyReport) -> Table:
    table = Table(title=f"organize apply {report.plan_id} ({report.link_mode})")
    for col in ("destination", "mode", "status"):
        table.add_column(col, overflow="fold")
    for row in report.applied:
        table.add_row(row["destination"], row["mode"], "applied")
    for row in report.skipped:
        table.add_row(row["destination"], "-", f"skipped: {row['status']}")
    for row in report.errors:
        table.add_row(row["file_id"], "-", f"[red]error: {row['error']}[/red]")
    return table


# --------------------------------------------------------------------------
# undo
# --------------------------------------------------------------------------
def _find_undo_log(settings: Settings, log_id: str | None) -> Path:
    undo_dir = settings.paths.undo_dir
    if log_id in (None, "", "latest"):
        found = _newest(undo_dir, "undo-*.json")
        if found is None:
            raise MitsyncError(f"no undo log in {undo_dir}; nothing to undo")
        return found
    assert log_id is not None
    for candidate in (
        Path(log_id),
        undo_dir / log_id,
        undo_dir / f"{log_id}.json",
        undo_dir / f"undo-{log_id}.json",
    ):
        if candidate.exists():
            return candidate
    raise MitsyncError(f"no undo log matching '{log_id}' in {undo_dir}")


def undo(settings: Settings, log_id: str | None = None) -> UndoReport:
    """Reverse an applied plan, refusing anything whose bytes changed since."""
    path = _find_undo_log(settings, log_id)
    doc = json.loads(path.read_text(encoding="utf-8"))
    report = UndoReport(log_id=doc.get("log_id") or path.stem)
    ws = settings.paths.workspace

    if doc.get("undone_at"):
        console.print(f"[yellow]{path.name} was already undone at {doc['undone_at']}.[/yellow]")
        return report

    # (uuid, filed_path, skip_reason) to write back, as in `apply_plan`.
    filing: list[tuple[str, str | None, str | None]] = []
    for op in reversed(doc.get("operations", [])):
        if op["mode"] == "skip":
            filing.append((op["uuid"], None, None))
            continue
        dest = ws / op["destination"]
        source = ws / op["source"]
        if op["mode"] == "displace":
            # Runs after the replacement was removed: put the displaced copy back.
            if dest.exists():
                report.refused.append(
                    {"path": op["destination"], "reason": "occupied; not restoring the old copy"}
                )
                continue
            _place(source, dest, doc["link_mode"])
            report.restored.append(op["destination"])
            filing.append((op["uuid"], op["destination"], None))
            continue
        if not dest.exists() and not dest.is_symlink():
            report.refused.append(
                {"path": op["destination"], "reason": "already gone; leaving the rest alone"}
            )
            continue
        expected = op.get("destination_sha256")
        if expected and not dest.is_symlink():
            if sha256_file(dest) != expected:
                report.refused.append(
                    {
                        "path": op["destination"],
                        "reason": "changed since the plan was applied; refusing to undo it",
                    }
                )
                continue
        try:
            if op["mode"] in ("move", "refile"):
                if source.exists():
                    report.refused.append(
                        {"path": op["source"], "reason": "original path is occupied again"}
                    )
                    continue
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), str(source))
                report.restored.append(op["source"])
            else:
                dest.unlink()
                report.removed.append(op["destination"])
        except OSError as exc:
            report.errors.append({"path": op["destination"], "error": str(exc)})
            continue
        _prune_empty(dest.parent, ws)
        if op.get("uuid"):
            filing.append((op["uuid"], op["source"] if op["mode"] == "refile" else None, None))

    _record_filing(settings, filing)

    if not report.errors and not report.refused:
        doc["undone_at"] = now_iso()
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    console.print(
        f"undo {report.log_id}: restored {len(report.restored)}, removed {len(report.removed)}, "
        f"refused {len(report.refused)}, errors {len(report.errors)}"
    )
    for row in report.refused:
        console.print(f"[yellow]refused[/yellow] {row['path']}: {row['reason']}")
    for row in report.errors:
        console.print(f"[red]error[/red] {row['path']}: {row['error']}")
    return report


def _prune_empty(directory: Path, workspace: Path) -> None:
    """Remove directories this tool emptied, never climbing past the workspace.

    Both failure modes used to return silently, which gave "I cannot read this
    directory" (a real problem, usually permissions) and "something appeared in
    it while I looked" (a benign macOS race -- Finder writing .DS_Store between
    the emptiness check and the rmdir) the same answer: none.

    Only the race is tolerated, and only by stopping the climb. Anything else
    raises, because a directory this tool cannot read or remove inside its own
    workspace is a state worth failing on.
    """
    current = directory
    while current != workspace and workspace in current.parents:
        if any(current.iterdir()):
            return
        try:
            current.rmdir()
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return  # lost the race; the directory is no longer ours to remove
            raise
        current = current.parent

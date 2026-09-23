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
    "reason"}]}` -- against its JSON schema and against the structural filing
    rules below, splitting it into accepted and rejected placements, each
    rejection with its reason.
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

**A name collision never overwrites.** An identical file already at the
destination is a skip that still records the file as filed; a different one
rejects the placement.

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
from mitsync.core.config import read_json
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
BUCKETS = ("lectures", "recitations", "assignments", "data", "syllabus", "notes", "other")

#: The subfolders a *new* placement may target (config/naming.md §2).
FILING_BUCKETS = ("lectures", "recitations", "assignments", "syllabus", "notes", "other")

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
        }
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
    """One accepted placement. ``uuid`` is empty for pre-existing files."""

    file_id: str
    uuid: str
    source: str
    destination: str
    reason: str
    kind: str = "canvas"  # canvas | existing


@dataclass
class Plan:
    """An agent-written plan after validation: what will run and what will not."""

    plan_id: str
    entries: list[PlanEntry] = field(default_factory=list)
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
                "display_name": rec.display_name or rec.filename,
                "course": folder_for_canvas_id(settings, rec.course_canvas_id),
                "mirror_course": rec.course_folder,
                "canvas_folder": rec.canvas_folder,
                "module_name": rec.module_name,
                "module_position": rec.module_position,
                "content_type": rec.content_type,
                "size": rec.size,
                "filed_path": rec.filed_path,
            }
    return out


def unfiled(settings: Settings) -> dict[str, Any]:
    """Every mirrored file not filed yet, plus the rules the agent files by.

    The agent reads `naming_rules` itself and writes a plan matching
    `plan_schema`; `organize apply --plan` validates and applies it.
    """
    files = [
        {k: v for k, v in c.items() if k not in ("uuid", "source", "filed_path")}
        | {"mirror_path": c["source"]}
        for c in _canvas_candidates(settings).values()
        if not c["filed_path"]
    ]
    files.sort(key=lambda f: (f["course"] or "", f["mirror_path"]))
    return {
        "naming_rules": str(naming_rules_path(settings)),
        "course_folders": existing_course_folders(settings),
        "buckets": list(FILING_BUCKETS),
        "per_item_buckets": list(PER_ITEM_BUCKETS),
        "existing_file_id_prefix": EXISTING_PREFIX,
        "plans_dir": str(settings.paths.plans_dir),
        "plan_schema": PLAN_SCHEMA,
        "files": files,
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


def _existing_source(settings: Settings, rel: str, courses: list[str]) -> str:
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
    return clean


def validate_plan(
    settings: Settings, plan_path: Path | str, *, include_existing: bool = False
) -> Plan:
    """Split an agent-written plan into accepted entries and rejections.

    Touches nothing on disk. Each rejection carries the placement's
    ``file_id``, its ``destination`` and the ``reason``.
    """
    path = Path(plan_path)
    doc = load_plan_document(path)
    the_plan = Plan(plan_id=path.stem, path=path)
    courses = existing_course_folders(settings)
    candidates = _canvas_candidates(settings)
    ws = settings.paths.workspace

    seen_ids: set[str] = set()
    seen_dests: set[str] = set()

    def reject(placement: dict[str, Any], reason: str) -> None:
        the_plan.rejected.append(
            {
                "file_id": placement["file_id"],
                "destination": placement["destination"],
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
                    _existing_source(settings, file_id[len(EXISTING_PREFIX) :], courses),
                    "",
                    "existing",
                )
            else:
                candidate = candidates.get(file_id)
                if candidate is None:
                    raise ValueError("no mirrored file with this file_id in the manifest")
                if candidate["filed_path"]:
                    raise ValueError(f"already filed at {candidate['filed_path']}")
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
            if target.exists() and sha256_file(target) != sha256_file(ws / source):
                raise ValueError("a different file already exists at the destination")
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
            )
        )
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
    what = f"{len(the_plan.entries)} placement(s)"
    if moves:
        what += f", {moves} of them MOVING a pre-existing file"
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

    if not the_plan.entries:
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
    filed: list[tuple[str, str]] = []

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
                    filed.append((entry.uuid, dest_rel))
                continue
            report.errors.append(
                {"file_id": entry.file_id, "error": "a different file appeared at the destination"}
            )
            continue

        src_sha = sha256_file(source)
        try:
            if entry.kind == "existing":
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))
                used = "move"
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
            filed.append((entry.uuid, dest_rel))

    report.undo_log = _write_undo_log(settings, the_plan, mode, operations)
    _record_filed(settings, filed)
    _print_report(report)
    if report.undo_log is not None:
        console.print(f"undo log: [bold]{report.undo_log}[/bold]")
    return report


def _record_filed(settings: Settings, filed: list[tuple[str, str]]) -> None:
    if not filed:
        return
    from mitsync.canvas.manifest import Manifest

    with Manifest(settings.paths.manifest_db) as man:
        for uuid, dest in filed:
            man.set_filed_path(uuid, dest)


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

    cleared: list[str] = []
    for op in reversed(doc.get("operations", [])):
        dest = ws / op["destination"]
        source = ws / op["source"]
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
            if op["mode"] == "move":
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
            cleared.append(op["uuid"])

    if cleared:
        from mitsync.canvas.manifest import Manifest

        with Manifest(settings.paths.manifest_db) as man:
            for uuid in cleared:
                man.set_filed_path(uuid, None)

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

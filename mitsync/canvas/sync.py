"""
# Canvas Sync

Mirror Canvas into `<workspace>/_canvas/` and record what was mirrored in the
manifest.

## 1. What This Module Does

Discovers the current term's courses, downloads every file they expose into an
immutable mirror, writes the course metadata beside it as JSON, and records
each file and course in the DuckDB manifest. It produces a `SyncReport` naming
everything that was skipped, missed, or failed.

This layer only *mirrors and records*. It never touches the student's own
human-named course folders -- `organize` does that later, reading the manifest
rows written here.

## 2. Why This Module Exists

`_canvas/` is the source of truth and is never emptied: filing hardlinks or
copies out of the mirror and never moves files out of it, so the mirror can
always be wiped and rebuilt without losing anything the student has curated.
Separating the mirror from the curated view is what makes the filing step safe
to re-run, review, and undo.

The metadata files exist so that everything downstream -- deadlines,
homework status, the course map -- can answer questions about Canvas without a
network call and without a Canvas token.

## 3. How It Fits in the Architecture

The only module that constructs a `CanvasClient`, and therefore the only one
that needs `httpx`. Below it: `canvas_client` for HTTP and `manifest` for
state. Above it: `organize` files what it mirrored, `deadlines` and
`course_map` read the `_meta` JSON it wrote.

`run_sync` never exits the process -- `cli.run()` is the only place a
`MitsyncError` becomes an exit code -- so tests and OpenClaw skills can call
this module directly.

## 4. Key Concepts

**The layout produced.**

    _canvas/
      _meta/planner.json                 # cross-course planner items
      <Course Folder>/
        <canvas folder path>/<filename>  # the mirrored bytes
        _meta/courses.json
        _meta/modules.json
        _meta/assignments.json
        _meta/pages.json
        _meta/announcements.json

**The metadata shape** (`deadlines.py` and friends read these; keep it
simple). Every `_meta` file is a JSON object with exactly three keys:

    {"fetched_at": "<ISO8601 UTC>", "course_canvas_id": <int|null>, "items": [...]}

`items` holds the Canvas payloads verbatim, in the order Canvas returned them.
`courses.json` holds one item -- the course object with its `term`, plus the
derived `mirror_folder` and `mirror_path`. `modules.json` holds module objects
including their `items` array (module order is the syllabus order; `position`
is authoritative). `assignments.json` is ordered by `due_at`. `pages.json`
includes each page's `body`. `announcements.json` covers the whole term, with
explicit start and end dates always sent, because Canvas's default window is
only -14d/+28d. `_canvas/_meta/planner.json` holds planner items across every
course, fetched once per run.

**Some module links are mirrored too.** Two kinds of module item have bytes
behind them that are not Canvas files:

* an `ExternalUrl` to a Google Slides deck shared by link, exported as PDF;
* an `ExternalTool` launching an HBS Publishing case or article
  (`hbsp.download_case`, which performs the LTI launch the student's click
  would).

Each is written to `<Course Folder>/<module name>/<item title>.pdf` and
recorded like a file, keyed `link-<module item id>` with `canvas_id` 0 (it is
not a Canvas file). Neither source gives a version signal, so a link is
fetched once and again only on `--full`. An answer that is not a PDF (a deck
not shared by link comes back as a sign-in page) is a notice, not an error.
Every other link (forms, websites) is never fetched.

**Catch-up safety.** Work is derived from manifest state versus Canvas state
-- never from time since the last run -- so a skipped or late scheduled run
costs nothing.

**Nothing is destroyed.** When a file's bytes change, the previous version is
parked beside the new one as `<stem>.v<UTC stamp><suffix>` rather than
overwritten. Downloads stage to a `.download` sibling and are renamed into
place.

**Term selection is a relative rule.** Canvas's "Default Term" carries no
dates and is used for administrative onboarding courses, so a dateless course
is dropped only when at least one *other* course sits in a properly dated
current term. On a Canvas instance that simply never sets term dates, nothing
is dropped. An explicit `canvas.term` bypasses the rule entirely, and
`canvas.exclude_courses` (a course id, or a case-insensitive substring of the
name or code) excludes courses by hand.

**Why exceptions are caught here.** Canvas is the one genuinely external
system mitsync talks to, and a term's worth of courses must not be lost
because one of them hid its Files tab. Two kinds of Canvas failure are handled
per stage, and nothing else is:

* `CanvasAccessDenied` / `CanvasFeatureDisabled` -- the course turned that tab
  off. Recorded with `add_notice` and skipped; never reported as an error, or
  the real errors drown in the noise.
* `_STAGE_FAILURES` -- a Canvas HTTP failure or a transport error. Recorded
  with `add_error` so the run finishes and the report names what was missed.
  The tuple is deliberately narrow.

`CanvasAuthError` and every non-Canvas `MitsyncError` propagate on purpose: a
bad token or a broken config is a whole-run failure, not a per-stage notice,
and filing a `ConfigError` away as a Canvas problem would hide a bug in
mitsync itself.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from rich.console import Console
from rich.table import Table

from mitsync.canvas import hbsp
from mitsync.canvas.client import CanvasClient
from mitsync.canvas.manifest import CourseRecord, FileRecord, Manifest, synthetic_uuid
from mitsync.core.clock import now_iso, parse_iso
from mitsync.core.config import read_meta
from mitsync.core.errors import (
    CanvasAccessDenied,
    CanvasFeatureDisabled,
    CanvasHTTPError,
    CanvasNotFound,
    CanvasRateLimited,
    MitsyncError,
)
from mitsync.core.logging import get_logger
from mitsync.core.paths import unique_path

log = get_logger(__name__)
console = Console()

__all__ = [
    "SyncReport",
    "course_exclusion_reason",
    "course_folder_name",
    "filter_excluded_courses",
    "read_meta",
    "run_sync",
    "select_current_term_courses",
]

#: Canvas-side failures one stage may record and keep walking past. Deliberately
#: narrow: a `CanvasAuthError` must abort the whole run rather than be logged
#: once per stage, and a non-Canvas `MitsyncError` (a `ConfigError`, say) is a
#: bug in mitsync that must not be filed away as a Canvas problem.
_STAGE_FAILURES = (CanvasHTTPError, CanvasNotFound, CanvasRateLimited, httpx.HTTPError)

_META_DIR = "_meta"
_GOOGLE_SLIDES = re.compile(r"^https://docs\.google\.com/presentation/d/([\w-]+)")
_TERM_PAD_DAYS = 200


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
@dataclass
class SyncReport:
    """What a sync run did (or, with ``dry_run``, would have done)."""

    courses: list[str] = field(default_factory=list)
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    bytes_downloaded: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    notices: list[dict[str, str]] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    dry_run: bool = False

    @property
    def files_seen(self) -> int:
        return self.new + self.updated + self.unchanged + self.skipped

    def add_excluded(self, canvas_id: Any, name: str, reason: str) -> None:
        """Record a course left out of this run, so the skip is visible."""
        log.info("excluded course %s (%s): %s", canvas_id, name, reason)
        self.excluded.append({"canvas_id": str(canvas_id), "name": name, "reason": reason})

    def add_error(self, course: str, stage: str, message: str) -> None:
        log.warning("%s [%s]: %s", course, stage, message)
        self.errors.append({"course": course, "stage": stage, "message": message})

    def add_notice(self, course: str, stage: str, message: str) -> None:
        """Record an expected, benign skip -- never an error.

        A course with a feature tab switched off (Pages disabled, Files
        hidden) is not a failure: there is simply nothing to mirror. Keeping
        these out of ``errors`` is what lets a real error stay visible.
        """
        log.info("%s [%s] skipped: %s", course, stage, message)
        self.notices.append({"course": course, "stage": stage, "message": message})

    def as_dict(self) -> dict[str, Any]:
        return {
            "courses": list(self.courses),
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "bytes_downloaded": self.bytes_downloaded,
            "errors": list(self.errors),
            "notices": list(self.notices),
            "excluded": list(self.excluded),
            "dry_run": self.dry_run,
        }

    def render(self) -> Table:
        title = "canvas sync (dry run)" if self.dry_run else "canvas sync"
        table = Table(title=title, show_lines=False)
        table.add_column("metric", style="bold")
        table.add_column("value", justify="right")
        table.add_row("courses", str(len(self.courses)))
        table.add_row("new" if not self.dry_run else "would download (new)", str(self.new))
        table.add_row(
            "updated" if not self.dry_run else "would download (updated)", str(self.updated)
        )
        table.add_row("unchanged", str(self.unchanged))
        table.add_row("skipped (ignored)", str(self.skipped))
        table.add_row("bytes", _human_bytes(self.bytes_downloaded))
        table.add_row("errors", str(len(self.errors)))
        table.add_row("skipped stages (expected)", str(len(self.notices)))
        table.add_row("excluded courses", str(len(self.excluded)))
        if self.courses:
            table.add_section()
            table.add_row("folders", ", ".join(self.courses))
        for item in self.excluded:
            table.add_section()
            table.add_row(f"excluded {item['canvas_id']}", f"{item['name']} — {item['reason']}")
        for notice in self.notices:
            table.add_section()
            table.add_row(
                f"[dim]skipped {notice['course']} / {notice['stage']}[/dim]",
                f"[dim]{notice['message']}[/dim]",
            )
        for err in self.errors:
            table.add_section()
            table.add_row(f"{err['course']} / {err['stage']}", err["message"])
        return table


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def run_sync(
    settings: Any,
    *,
    course: str | None = None,
    dry_run: bool = False,
    full: bool = False,
    client: CanvasClient | None = None,
) -> SyncReport:
    """Mirror Canvas for every current-term course (or just ``course``).

    ``client`` is a seam for tests (pass a :class:`CanvasClient` built on an
    ``httpx.MockTransport``); production callers leave it ``None``.
    """
    started = now_iso()
    report = SyncReport(dry_run=dry_run)
    paths = settings.paths
    paths.ensure()

    owns_client = client is None
    if client is None:
        client = CanvasClient(settings)

    # In dry-run mode never create a database file that did not already exist.
    in_memory = dry_run and not Path(paths.manifest_db).exists()
    manifest = Manifest(Path(":memory:") if in_memory else paths.manifest_db)
    try:
        _sync_all(settings, client, manifest, report, course=course, dry_run=dry_run, full=full)
    finally:
        if owns_client:
            client.close()
        if not dry_run:
            manifest.record_run("sync", started, now_iso(), report.as_dict())
        manifest.close()

    console.print(report.render())
    return report


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
def _sync_all(
    settings: Any,
    client: CanvasClient,
    manifest: Manifest,
    report: SyncReport,
    *,
    course: str | None,
    dry_run: bool,
    full: bool,
) -> None:
    try:
        courses = _discover_courses(settings, client, report)
    except MitsyncError as exc:
        report.add_error("-", "courses", str(exc))
        return
    except httpx.HTTPError as exc:
        report.add_error("-", "courses", f"{type(exc).__name__}: {exc}")
        return

    if course:
        target = course.strip().casefold()
        courses = [
            c
            for c in courses
            if any(
                text.strip().casefold() == target
                for text in (c["folder"], c["name"], c.get("course_code") or "")
                if text
            )
        ]
        if not courses:
            report.add_error("-", "courses", f"no current-term course matches {course!r}")
            return

    term_start, term_end = _term_window(courses)
    for entry in courses:
        report.courses.append(entry["folder"])
        if not dry_run:
            term = entry.get("term") or {}
            manifest.upsert_course(
                CourseRecord(
                    canvas_id=entry["canvas_id"],
                    name=entry["name"],
                    course_code=entry.get("course_code"),
                    term_name=term.get("name"),
                    term_start=term.get("start_at"),
                    term_end=term.get("end_at"),
                    folder=entry["folder"],
                )
            )
        _sync_course(
            settings,
            client,
            manifest,
            report,
            entry,
            term_start,
            term_end,
            dry_run=dry_run,
            full=full,
        )

    _write_planner(settings, client, report, term_start, term_end, dry_run=dry_run)


def _sync_course(
    settings: Any,
    client: CanvasClient,
    manifest: Manifest,
    report: SyncReport,
    entry: dict[str, Any],
    term_start: date,
    term_end: date,
    *,
    dry_run: bool,
    full: bool,
) -> None:
    folder = entry["folder"]
    course_id = entry["canvas_id"]

    folder_names = _folder_map(client, course_id, report, folder)
    files: dict[int, dict[str, Any]] = {}

    try:
        for raw in client.paginate(f"/courses/{course_id}/files"):
            files[int(raw["id"])] = dict(raw)
    except (CanvasAccessDenied, CanvasFeatureDisabled) as exc:
        # The Files tab is hidden (403) or disabled (404). Expected, and the
        # Modules walk below reaches the same files -- a notice, not an error.
        report.add_notice(folder, "files", f"{exc}; falling back to module-derived files")
    except _STAGE_FAILURES as exc:
        report.add_error(folder, "files", f"{type(exc).__name__}: {exc}")

    modules = _walk_modules(client, course_id, report, folder, files)

    for raw in files.values():
        _sync_file(
            settings, client, manifest, report, entry, raw, folder_names, dry_run=dry_run, full=full
        )
    for module in modules:
        for item in module.get("items") or []:
            url = str(item.get("external_url") or "")
            slides = _GOOGLE_SLIDES.match(url)
            if item.get("type") == "ExternalUrl" and slides:
                export = f"https://docs.google.com/presentation/d/{slides.group(1)}/export/pdf"
                fetch = partial(client.download_url, export)
            elif item.get("type") == "ExternalTool" and urlparse(url).netloc == hbsp.HBSP_HOST:
                fetch = partial(hbsp.download_case, client, item)
            else:
                continue  # documented: other links have no bytes we may fetch
            _sync_link(
                settings, manifest, report, entry, module, item, fetch, dry_run=dry_run, full=full
            )

    _write_course_meta(
        settings, client, report, entry, modules, term_start, term_end, dry_run=dry_run
    )


# --------------------------------------------------------------------------
# course discovery
# --------------------------------------------------------------------------
def _discover_courses(
    settings: Any, client: CanvasClient, report: SyncReport | None = None
) -> list[dict[str, Any]]:
    wanted = settings.canvas.term
    raws: list[dict[str, Any]] = []
    seen: set[int] = set()
    params = {"enrollment_state": "active", "include[]": ["term"]}
    for raw in client.paginate("/courses", **params):
        canvas_id = int(raw["id"])
        if canvas_id in seen:
            # Canvas pages can overlap when an enrolment changes mid-walk.
            continue
        seen.add(canvas_id)
        raws.append(raw)

    kept, dropped = filter_excluded_courses(raws, settings.canvas.exclude_courses)
    kept, term_dropped = select_current_term_courses(kept, wanted)
    if report is not None:
        for item in [*dropped, *term_dropped]:
            report.add_excluded(item["canvas_id"], item["name"], item["reason"])

    out: list[dict[str, Any]] = []
    for raw in kept:
        canvas_id = int(raw["id"])
        name = course_display_name(raw)
        out.append(
            {
                "canvas_id": canvas_id,
                "name": name,
                "course_code": raw.get("course_code"),
                "term": raw.get("term") or {},
                "folder": course_folder_name(name, canvas_id),
                "raw": raw,
            }
        )
    return out


def course_display_name(raw: dict[str, Any]) -> str:
    canvas_id = raw.get("id")
    return raw.get("name") or raw.get("course_code") or f"course-{canvas_id}"


# --------------------------------------------------------------------------
# course selection
# --------------------------------------------------------------------------
EXCLUDED_BY_CONFIG = "excluded by config"
EXCLUDED_DATELESS_TERM = "term has no start/end dates"


def course_exclusion_reason(course: dict[str, Any], patterns: Any) -> str | None:
    """``EXCLUDED_BY_CONFIG`` when ``course`` matches a ``canvas.exclude_courses`` entry.

    An entry that is an int or a numeric string matches the Canvas course id;
    anything else is a case-insensitive substring of the name or course code.
    Works on both raw Canvas payloads (``id``) and mapped rows (``canvas_id``).
    """
    canvas_id = course.get("id", course.get("canvas_id"))
    haystacks = [
        str(course.get("name") or "").casefold(),
        str(course.get("course_code") or "").casefold(),
    ]
    for pattern in patterns or []:
        text = str(pattern).strip()
        if not text:
            continue
        if text.lstrip("-").isdigit():
            if canvas_id is not None and str(canvas_id).strip() == text:
                return EXCLUDED_BY_CONFIG
            continue
        needle = text.casefold()
        if any(needle in hay for hay in haystacks if hay):
            return EXCLUDED_BY_CONFIG
    return None


def filter_excluded_courses(
    courses: list[dict[str, Any]], patterns: Any
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Split ``courses`` into (kept, dropped) per ``canvas.exclude_courses``."""
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for course in courses:
        reason = course_exclusion_reason(course, patterns)
        if reason is None:
            kept.append(course)
        else:
            dropped.append(
                {
                    "canvas_id": str(course.get("id", course.get("canvas_id")) or ""),
                    "name": course_display_name(course),
                    "reason": reason,
                }
            )
    return kept, dropped


def select_current_term_courses(
    courses: list[dict[str, Any]], wanted: str = "auto"
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Pick the current-term courses out of the whole list.

    The decision needs every course at once: a term carrying no dates at all
    (Canvas's "Default Term", used for administrative onboarding courses) is
    only excluded when at least one *other* course sits in a properly dated
    current term. That keeps the rule relative -- on a Canvas instance that
    simply never sets term dates, nothing is dropped. An explicit
    ``canvas.term`` name bypasses the rule entirely.
    """
    if wanted and wanted != "auto":
        return [c for c in courses if _in_term(c.get("term") or {}, wanted)], []

    dated_current: list[dict[str, Any]] = []
    dateless: list[dict[str, Any]] = []
    for course in courses:
        term = course.get("term") or {}
        if _as_date(term.get("start_at")) is None and _as_date(term.get("end_at")) is None:
            dateless.append(course)
        elif _in_term(term, wanted):
            dated_current.append(course)

    if not dated_current:
        # Never return an empty list because of this rule.
        return dateless, []

    dropped = [
        {
            "canvas_id": str(c.get("id", c.get("canvas_id")) or ""),
            "name": course_display_name(c),
            "reason": EXCLUDED_DATELESS_TERM,
        }
        for c in dateless
    ]
    return dated_current, dropped


def _in_term(term: dict[str, Any], wanted: str) -> bool:
    """Canvas has no current-term filter, so bracket ``today`` client-side."""
    if wanted and wanted != "auto":
        name = (term.get("name") or "").strip().casefold()
        return name == wanted.strip().casefold()
    today = datetime.now(UTC).date()
    start = _as_date(term.get("start_at"))
    end = _as_date(term.get("end_at"))
    if start is None and end is None:
        # Handled by `select_current_term_courses`, which sees every course.
        return True
    if start is not None and today < start:
        return False
    if end is not None and today > end:
        return False
    return True


def _term_window(courses: list[dict[str, Any]]) -> tuple[date, date]:
    """Widest start/end across the selected terms, padded when unknown."""
    today = datetime.now(UTC).date()
    starts = [d for d in (_as_date((c.get("term") or {}).get("start_at")) for c in courses) if d]
    ends = [d for d in (_as_date((c.get("term") or {}).get("end_at")) for c in courses) if d]
    start = min(starts) if starts else today - timedelta(days=_TERM_PAD_DAYS)
    end = max(ends) if ends else today + timedelta(days=_TERM_PAD_DAYS)
    return start, end


# --------------------------------------------------------------------------
# folders, files, modules
# --------------------------------------------------------------------------
def _folder_map(
    client: CanvasClient, course_id: int, report: SyncReport, folder: str
) -> dict[int, str]:
    """``folder_id -> relative folder path`` (the ``course files`` root strips out)."""
    out: dict[int, str] = {}
    try:
        for raw in client.paginate(f"/courses/{course_id}/folders"):
            parts = [p for p in str(raw.get("full_name") or raw.get("name") or "").split("/") if p]
            if parts and parts[0].casefold() == "course files":
                parts = parts[1:]  # the Canvas root folder is not part of the mirror path
            out[int(raw["id"])] = "/".join(_sanitize_component(p) for p in parts)
    except (CanvasAccessDenied, CanvasFeatureDisabled) as exc:
        log.info("%s: folder listing unavailable (%s)", folder, exc)
    except _STAGE_FAILURES as exc:
        report.add_error(folder, "folders", f"{type(exc).__name__}: {exc}")
    return out


def _walk_modules(
    client: CanvasClient,
    course_id: int,
    report: SyncReport,
    folder: str,
    files: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Walk modules even when ``/files`` worked: module order is the filing signal."""
    modules: list[dict[str, Any]] = []
    try:
        modules = list(client.paginate(f"/courses/{course_id}/modules", **{"include[]": ["items"]}))
    except (CanvasAccessDenied, CanvasFeatureDisabled) as exc:
        report.add_notice(folder, "modules", str(exc))
        return modules
    except _STAGE_FAILURES as exc:
        report.add_error(folder, "modules", f"{type(exc).__name__}: {exc}")
        return modules

    for module in modules:
        name = module.get("name")
        position = module.get("position")
        for item in module.get("items") or []:
            # Documented contract: a module lists Pages, Quizzes and links too;
            # only File items have bytes to mirror.
            if item.get("type") != "File":
                continue
            fid = int(item["content_id"])
            raw = files.get(fid)
            if raw is None:
                try:
                    raw = client.get(f"/files/{fid}")
                except (CanvasAccessDenied, CanvasFeatureDisabled) as exc:
                    report.add_notice(folder, "module-file", str(exc))
                    continue
                except _STAGE_FAILURES as exc:
                    report.add_error(folder, "module-file", f"{type(exc).__name__}: {exc}")
                    continue
                raw = dict(raw)
                files[fid] = raw
            # First module wins: a file referenced twice keeps its earliest slot.
            raw.setdefault("_module_name", name)
            raw.setdefault("_module_position", position)
    return modules


# --------------------------------------------------------------------------
# one file
# --------------------------------------------------------------------------
def _sync_file(
    settings: Any,
    client: CanvasClient,
    manifest: Manifest,
    report: SyncReport,
    entry: dict[str, Any],
    raw: dict[str, Any],
    folder_names: dict[int, str],
    *,
    dry_run: bool,
    full: bool,
) -> None:
    course_folder = entry["folder"]
    canvas_id = int(raw["id"])
    uuid = raw.get("uuid") or synthetic_uuid(canvas_id)
    filename = _sanitize_component(
        raw.get("filename") or raw.get("display_name") or f"file-{canvas_id}"
    )
    display_name = raw.get("display_name") or filename
    canvas_folder = folder_names.get(raw.get("folder_id")) or None

    rel_parts = ["_canvas", course_folder]
    if canvas_folder:
        rel_parts.extend(canvas_folder.split("/"))
    rel_parts.append(filename)
    rel_path = "/".join(rel_parts)

    if settings.should_ignore(rel_path):
        report.skipped += 1
        log.debug("ignored by glob: %s", rel_path)
        return

    dest = settings.paths.workspace / Path(rel_path)
    updated_at = str(raw.get("updated_at") or "")
    existing = manifest.get_file(uuid)

    if not full and existing is not None and existing.updated_at == updated_at and dest.exists():
        report.unchanged += 1
        return

    is_new = existing is None
    if dry_run:
        if is_new:
            report.new += 1
        else:
            report.updated += 1
        log.info("would download %s", rel_path)
        return

    staging = dest.with_name(dest.name + ".download")
    try:
        sha = client.download(canvas_id, staging)
    except (MitsyncError, httpx.HTTPError) as exc:
        staging.unlink(missing_ok=True)
        report.add_error(course_folder, "download", f"{display_name}: {type(exc).__name__}: {exc}")
        return

    size = staging.stat().st_size
    report.bytes_downloaded += size
    identical = existing is not None and existing.sha256 == sha
    if dest.exists() and not identical:
        # Never destroy data: park the old bytes beside the new ones.
        _keep_previous_version(dest)
    staging.replace(dest)

    if is_new:
        report.new += 1
    elif identical and existing is not None and existing.updated_at == updated_at:
        report.unchanged += 1
    else:
        report.updated += 1

    now = now_iso()
    manifest.upsert_file(
        FileRecord(
            uuid=uuid,
            canvas_id=canvas_id,
            course_folder=course_folder,
            course_canvas_id=entry["canvas_id"],
            display_name=display_name,
            filename=filename,
            content_type=raw.get("content_type") or raw.get("mime_class"),
            size=raw.get("size") if raw.get("size") is not None else size,
            canvas_folder=canvas_folder,
            module_name=raw.get("_module_name"),
            module_position=raw.get("_module_position"),
            updated_at=updated_at,
            sha256=sha,
            mirror_path=rel_path,
            filed_path=None,
            first_seen=existing.first_seen if existing else now,
            last_synced=now,
        )
    )


def _sync_link(
    settings: Any,
    manifest: Manifest,
    report: SyncReport,
    entry: dict[str, Any],
    module: dict[str, Any],
    item: dict[str, Any],
    fetch: Callable[[Path], str],
    *,
    dry_run: bool,
    full: bool,
) -> None:
    """Mirror one module link as a PDF; ``fetch(path)`` writes it and returns its
    sha256 (see "Some module links are mirrored too").
    """
    course_folder = entry["folder"]
    uuid = f"link-{item['id']}"
    title = str(item.get("title") or f"slides-{item['id']}")
    filename = _sanitize_component(title) + ".pdf"
    rel_path = "/".join(
        ["_canvas", course_folder, _sanitize_component(str(module.get("name"))), filename]
    )
    dest = settings.paths.workspace / Path(rel_path)
    existing = manifest.get_file(uuid)
    if not full and existing is not None and dest.exists():
        report.unchanged += 1
        return
    if dry_run:
        report.new += 1
        log.info("would fetch %s", rel_path)
        return

    staging = dest.with_name(dest.name + ".download")
    try:
        sha = fetch(staging)
    except (MitsyncError, httpx.HTTPError) as exc:
        staging.unlink(missing_ok=True)
        report.add_error(course_folder, "module-link", f"{title}: {type(exc).__name__}: {exc}")
        return
    with open(staging, "rb") as fh:
        is_pdf = fh.read(5) == b"%PDF-"
    if not is_pdf:
        staging.unlink()
        report.add_notice(
            course_folder, "module-link", f"{title}: not a PDF (not shared?); skipped"
        )
        return

    size = staging.stat().st_size
    report.bytes_downloaded += size
    if dest.exists() and not (existing is not None and existing.sha256 == sha):
        _keep_previous_version(dest)
    staging.replace(dest)
    if existing is None:
        report.new += 1
    else:
        report.updated += 1

    now = now_iso()
    manifest.upsert_file(
        FileRecord(
            uuid=uuid,
            canvas_id=0,
            course_folder=course_folder,
            course_canvas_id=entry["canvas_id"],
            display_name=title,
            filename=filename,
            content_type="application/pdf",
            size=size,
            canvas_folder=None,
            module_name=module.get("name"),
            module_position=module.get("position"),
            updated_at="",
            sha256=sha,
            mirror_path=rel_path,
            filed_path=None,
            first_seen=existing.first_seen if existing else now,
            last_synced=now,
        )
    )


def _keep_previous_version(dest: Path) -> None:
    """Never destroy data: park the old bytes as ``<stem>.v<UTC stamp><suffix>``."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archived = unique_path(dest.with_name(f"{dest.stem}.v{stamp}{dest.suffix}"))
    dest.rename(archived)
    log.info("kept previous version as %s", archived.name)


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------
def _write_course_meta(
    settings: Any,
    client: CanvasClient,
    report: SyncReport,
    entry: dict[str, Any],
    modules: list[dict[str, Any]],
    term_start: date,
    term_end: date,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    folder = entry["folder"]
    course_id = entry["canvas_id"]
    meta_dir = settings.paths.canvas_mirror / folder / _META_DIR

    course_item = dict(entry["raw"])
    course_item["mirror_folder"] = folder
    course_item["mirror_path"] = f"_canvas/{folder}"
    _write_meta(meta_dir / "courses.json", course_id, [course_item])
    _write_meta(meta_dir / "modules.json", course_id, modules)

    fetches: list[tuple[str, str, dict[str, Any]]] = [
        (
            "assignments",
            f"/courses/{course_id}/assignments",
            # `submission` is the caller's own submission. Without it the only
            # status field is `has_submitted_submissions`, which is course-wide.
            {"order_by": "due_at", "include[]": ["submission"]},
        ),
        ("pages", f"/courses/{course_id}/pages", {"include[]": ["body"]}),
        (
            "announcements",
            "/announcements",
            {
                "context_codes[]": [f"course_{course_id}"],
                "start_date": term_start.isoformat(),
                "end_date": term_end.isoformat(),
            },
        ),
    ]
    for stage, path, params in fetches:
        try:
            items = list(client.paginate(path, **params))
        except CanvasFeatureDisabled as exc:
            # e.g. Pages disabled for this course: 404 + "That page has been
            # disabled for this course". Nothing to mirror, nothing wrong.
            report.add_notice(folder, stage, str(exc))
            items = []
        except CanvasAccessDenied as exc:
            report.add_notice(folder, stage, str(exc))
            items = []
        except _STAGE_FAILURES as exc:
            report.add_error(folder, stage, f"{type(exc).__name__}: {exc}")
            items = []
        _write_meta(meta_dir / f"{stage}.json", course_id, items)


def _write_planner(
    settings: Any,
    client: CanvasClient,
    report: SyncReport,
    term_start: date,
    term_end: date,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    try:
        items = list(
            client.paginate(
                "/planner/items",
                start_date=term_start.isoformat(),
                end_date=term_end.isoformat(),
            )
        )
    except (CanvasAccessDenied, CanvasFeatureDisabled) as exc:
        report.add_notice("-", "planner", str(exc))
        return
    except _STAGE_FAILURES as exc:
        report.add_error("-", "planner", f"{type(exc).__name__}: {exc}")
        return
    _write_meta(settings.paths.canvas_mirror / _META_DIR / "planner.json", None, items)


def _write_meta(path: Path, course_canvas_id: int | None, items: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": now_iso(),
        "course_canvas_id": course_canvas_id,
        "items": items,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    tmp.replace(path)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_WS = re.compile(r"\s+")


def _sanitize_component(name: str) -> str:
    """One filesystem-safe path component: no separators, collapsed whitespace."""
    cleaned = unicodedata.normalize("NFC", str(name))
    cleaned = _UNSAFE.sub("-", cleaned)
    cleaned = _WS.sub(" ", cleaned).strip().strip(".")
    return cleaned[:150] or "untitled"


def course_folder_name(name: str, canvas_id: int | None = None) -> str:
    """Stable mirror folder name for a Canvas course name."""
    folder = _sanitize_component(name)
    if folder == "untitled" and canvas_id is not None:
        folder = f"course-{canvas_id}"
    return folder


def _as_date(value: Any) -> date | None:
    """A Canvas term bound as a date; `None` only when the term has no bound.

    Canvas always sends ISO 8601 here. Anything else is a Canvas-side change
    worth reporting, so it is translated rather than absorbed into `None`.
    """
    if not value:
        return None
    try:
        return parse_iso(value).date()
    except ValueError as exc:
        raise MitsyncError(f"Canvas sent an unparseable term date {value!r}: {exc}") from exc


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024

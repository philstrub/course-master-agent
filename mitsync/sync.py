"""Mirror Canvas into ``<workspace>/_canvas/`` and record it in the manifest.

This layer only *mirrors and records*. It never touches the student's own
human-named course folders — :mod:`mitsync.organize` does that later, reading
the manifest rows written here.

Layout produced
---------------
::

    _canvas/
      _meta/planner.json                 # cross-course planner items
      <Course Folder>/
        <canvas folder path>/<filename>  # the mirrored bytes
        _meta/courses.json
        _meta/modules.json
        _meta/assignments.json
        _meta/pages.json
        _meta/announcements.json

Metadata shapes (``deadlines.py`` and friends read these; keep them simple)
--------------------------------------------------------------------------
Every ``_meta`` file is a JSON object with exactly three keys::

    {"fetched_at": "<ISO8601 UTC>", "course_canvas_id": <int|null>, "items": [...]}

``items`` holds the Canvas payloads verbatim, as returned by the API, in the
order Canvas returned them:

``courses.json``
    one item: the course object (with its ``term``), plus the two derived keys
    ``mirror_folder`` and ``mirror_path``.
``modules.json``
    module objects including their ``items`` array (module order is the
    syllabus order; ``position`` is authoritative).
``assignments.json``
    assignment objects ordered by ``due_at``.
``pages.json``
    page objects including ``body``.
``announcements.json``
    announcement objects over the whole term (explicit start/end dates are
    always sent — Canvas's default window is only -14d/+28d).
``_canvas/_meta/planner.json``
    planner items across every course, fetched once per run.

Catch-up safety
---------------
Work is derived from manifest state versus Canvas state — never from "time
since the last run" — so a skipped or late scheduled run costs nothing.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.table import Table

from .canvas_client import CanvasClient
from .errors import CanvasAccessDenied, CanvasAuthError, MitsyncError
from .logging import get_logger
from .manifest import CourseRecord, FileRecord, Manifest, synthetic_uuid

log = get_logger(__name__)
console = Console()

__all__ = ["SyncReport", "course_folder_name", "run_sync"]

_META_DIR = "_meta"
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
    dry_run: bool = False

    @property
    def files_seen(self) -> int:
        return self.new + self.updated + self.unchanged + self.skipped

    def add_error(self, course: str, stage: str, message: str) -> None:
        log.warning("%s [%s]: %s", course, stage, message)
        self.errors.append({"course": course, "stage": stage, "message": message})

    def as_dict(self) -> dict[str, Any]:
        return {
            "courses": list(self.courses),
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "bytes_downloaded": self.bytes_downloaded,
            "errors": list(self.errors),
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
        if self.courses:
            table.add_section()
            table.add_row("folders", ", ".join(self.courses))
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
    started = _now()
    report = SyncReport(dry_run=dry_run)
    paths = settings.paths
    paths.ensure()

    owns_client = client is None
    if client is None:
        try:
            client = CanvasClient(settings)
        except CanvasAuthError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

    manifest = _open_manifest(paths.manifest_db, dry_run=dry_run)
    try:
        _sync_all(settings, client, manifest, report, course=course, dry_run=dry_run, full=full)
    finally:
        if owns_client:
            client.close()
        if not dry_run:
            manifest.record_run("sync", started, _now(), report.as_dict())
        manifest.close()

    console.print(report.render())
    return report


def _open_manifest(db_path: Path, *, dry_run: bool) -> Manifest:
    """In dry-run mode never create a database file that did not already exist."""
    if dry_run and not Path(db_path).exists():
        return Manifest(Path(":memory:"))
    return Manifest(db_path)


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
        courses = _discover_courses(settings, client)
    except MitsyncError as exc:
        report.add_error("-", "courses", str(exc))
        return
    except httpx.HTTPError as exc:
        report.add_error("-", "courses", f"{type(exc).__name__}: {exc}")
        return

    if course:
        courses = [c for c in courses if _matches_course(c, course)]
        if not courses:
            report.add_error("-", "courses", f"no current-term course matches {course!r}")
            return

    term_start, term_end = _term_window(courses)
    for entry in courses:
        report.courses.append(entry["folder"])
        if not dry_run:
            manifest.upsert_course(_course_record(entry))
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
    except CanvasAccessDenied as exc:
        report.add_error(folder, "files", f"{exc}; falling back to module-derived files")
    except (MitsyncError, httpx.HTTPError) as exc:
        report.add_error(folder, "files", f"{type(exc).__name__}: {exc}")

    modules = _walk_modules(client, course_id, report, folder, files)

    for raw in files.values():
        _sync_file(
            settings, client, manifest, report, entry, raw, folder_names, dry_run=dry_run, full=full
        )

    _write_course_meta(
        settings, client, report, entry, modules, term_start, term_end, dry_run=dry_run
    )


# --------------------------------------------------------------------------
# course discovery
# --------------------------------------------------------------------------
def _discover_courses(settings: Any, client: CanvasClient) -> list[dict[str, Any]]:
    wanted = settings.canvas.term
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    params = {"enrollment_state": "active", "include[]": ["term"]}
    for raw in client.paginate("/courses", **params):
        canvas_id = raw.get("id")
        if canvas_id is None or int(canvas_id) in seen:
            continue
        if not _in_term(raw.get("term") or {}, wanted):
            continue
        seen.add(int(canvas_id))
        name = raw.get("name") or raw.get("course_code") or f"course-{canvas_id}"
        out.append(
            {
                "canvas_id": int(canvas_id),
                "name": name,
                "course_code": raw.get("course_code"),
                "term": raw.get("term") or {},
                "folder": course_folder_name(name, int(canvas_id)),
                "raw": raw,
            }
        )
    return out


def _in_term(term: dict[str, Any], wanted: str) -> bool:
    """Canvas has no current-term filter, so bracket ``today`` client-side."""
    if wanted and wanted != "auto":
        name = (term.get("name") or "").strip().casefold()
        return name == wanted.strip().casefold()
    today = datetime.now(UTC).date()
    start = _as_date(term.get("start_at"))
    end = _as_date(term.get("end_at"))
    if start is None and end is None:
        # A term with no dates (e.g. "Default Term") cannot be excluded safely.
        return True
    if start is not None and today < start:
        return False
    if end is not None and today > end:
        return False
    return True


def _matches_course(entry: dict[str, Any], needle: str) -> bool:
    target = needle.strip().casefold()
    candidates = [entry["folder"], entry["name"], entry.get("course_code") or ""]
    return any(c.strip().casefold() == target for c in candidates if c)


def _course_record(entry: dict[str, Any]) -> CourseRecord:
    term = entry.get("term") or {}
    return CourseRecord(
        canvas_id=entry["canvas_id"],
        name=entry["name"],
        course_code=entry.get("course_code"),
        term_name=term.get("name"),
        term_start=term.get("start_at"),
        term_end=term.get("end_at"),
        folder=entry["folder"],
    )


def _term_window(courses: list[dict[str, Any]]) -> tuple[date, date]:
    """Widest start/end across the selected terms, padded when unknown."""
    today = datetime.now(UTC).date()
    starts = [d for d in (_as_date((c.get("term") or {}).get("start_at")) for c in courses) if d]
    ends = [d for d in (_as_date((c.get("term") or {}).get("end_at")) for c in courses) if d]
    start = min(starts) if starts else today - timedelta(days=_TERM_PAD_DAYS)
    end = max(ends) if ends else today + timedelta(days=_TERM_PAD_DAYS)
    if end < start:  # pragma: no cover - defensive
        end = start + timedelta(days=_TERM_PAD_DAYS)
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
            fid = raw.get("id")
            if fid is None:
                continue
            out[int(fid)] = _relative_folder(raw.get("full_name") or raw.get("name") or "")
    except CanvasAccessDenied as exc:
        log.info("%s: folder listing unavailable (%s)", folder, exc)
    except (MitsyncError, httpx.HTTPError) as exc:
        report.add_error(folder, "folders", f"{type(exc).__name__}: {exc}")
    return out


def _relative_folder(full_name: str) -> str:
    parts = [p for p in str(full_name).split("/") if p]
    if parts and parts[0].casefold() == "course files":
        parts = parts[1:]
    return "/".join(_sanitize_component(p) for p in parts)


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
    except CanvasAccessDenied as exc:
        report.add_error(folder, "modules", str(exc))
        return modules
    except (MitsyncError, httpx.HTTPError) as exc:
        report.add_error(folder, "modules", f"{type(exc).__name__}: {exc}")
        return modules

    for module in modules:
        name = module.get("name")
        position = module.get("position")
        for item in module.get("items") or []:
            if item.get("type") != "File":
                continue
            content_id = item.get("content_id")
            if content_id is None:
                continue
            fid = int(content_id)
            raw = files.get(fid)
            if raw is None:
                try:
                    raw = client.get(f"/files/{fid}")
                except CanvasAccessDenied as exc:
                    report.add_error(folder, "module-file", str(exc))
                    continue
                except (MitsyncError, httpx.HTTPError) as exc:
                    report.add_error(folder, "module-file", f"{type(exc).__name__}: {exc}")
                    continue
                if not isinstance(raw, dict) or "id" not in raw:
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

    now = _now()
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


def _keep_previous_version(dest: Path) -> None:
    """Never destroy data: park the old bytes as ``<stem>.v<UTC stamp><suffix>``."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archived = dest.with_name(f"{dest.stem}.v{stamp}{dest.suffix}")
    n = 1
    while archived.exists():
        archived = dest.with_name(f"{dest.stem}.v{stamp}-{n}{dest.suffix}")
        n += 1
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
        ("assignments", f"/courses/{course_id}/assignments", {"order_by": "due_at"}),
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
        except CanvasAccessDenied as exc:
            log.info("%s: %s unavailable (%s)", folder, stage, exc)
            items = []
        except (MitsyncError, httpx.HTTPError) as exc:
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
    except CanvasAccessDenied as exc:
        log.info("planner items unavailable (%s)", exc)
        return
    except (MitsyncError, httpx.HTTPError) as exc:
        report.add_error("-", "planner", f"{type(exc).__name__}: {exc}")
        return
    _write_meta(settings.paths.canvas_mirror / _META_DIR / "planner.json", None, items)


def _write_meta(path: Path, course_canvas_id: int | None, items: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": _now(),
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
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover

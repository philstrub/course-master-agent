"""What is due, and the daily briefing that says so.

Everything here is derived from state already on disk -- the ``_meta/*.json``
files :mod:`mitsync.sync` wrote, the manifest, and (best-effort) the calendar.
Nothing calls the network, and nothing is computed from "time since the last
run", so a laptop that was shut for a week catches up correctly instead of
skipping the window it missed.

Both outputs degrade rather than crash: a missing calendar, a missing
``_meta`` file or an empty manifest becomes a reported gap in the briefing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

from .errors import CalendarAccessDenied, MitsyncError
from .logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings

log = get_logger(__name__)
console = Console()

__all__ = ["DueReport", "build_due", "write_briefing"]

BRIEFING_WINDOW_DAYS = 7


@dataclass
class DueReport:
    items: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    calendar_ok: bool = False
    last_sync: str | None = None
    path: Path | None = None


# --------------------------------------------------------------------------
# reading what sync left behind
# --------------------------------------------------------------------------
def _read_meta(path: Path, warnings: list[str]) -> list[dict[str, Any]]:
    if not path.exists():
        warnings.append(f"missing {path.name} ({path}) — run `mitsync sync`")
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"cannot read {path}: {exc}")
        return []
    items = doc.get("items") if isinstance(doc, dict) else doc
    return [i for i in items or [] if isinstance(i, dict)]


def _course_folders(settings: Settings) -> dict[str, str]:
    """Mirror folder -> the student's folder name (falling back to itself)."""
    from .organize import load_course_map

    mapping: dict[str, str] = {}
    try:
        entries = load_course_map(settings)
    except MitsyncError as exc:
        log.warning("course map unreadable (%s)", exc)
        entries = []
    by_canvas_id = {str(e.get("canvas_id")): str(e.get("folder") or "") for e in entries}

    for meta in sorted(settings.paths.canvas_mirror.glob("*/_meta/courses.json")):
        mirror = meta.parent.parent.name
        course_id = None
        try:
            doc = json.loads(meta.read_text(encoding="utf-8"))
            course_id = str(doc.get("course_canvas_id") or "")
        except (OSError, json.JSONDecodeError):
            pass
        mapping[mirror] = by_canvas_id.get(course_id or "", "") or mirror
    return mapping


def _course_by_canvas_id(settings: Settings) -> dict[str, str]:
    from .organize import load_course_map

    out: dict[str, str] = {}
    try:
        for entry in load_course_map(settings):
            if entry.get("canvas_id") is not None and entry.get("folder"):
                out[str(entry["canvas_id"])] = str(entry["folder"])
    except MitsyncError:
        pass
    for meta in sorted(settings.paths.canvas_mirror.glob("*/_meta/courses.json")):
        try:
            doc = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        course_id = str(doc.get("course_canvas_id") or "")
        if course_id and course_id not in out:
            out[course_id] = meta.parent.parent.name
    return out


def _submitted(item: dict[str, Any]) -> bool:
    submission = item.get("submission")
    if isinstance(submission, dict):
        return bool(submission.get("submitted_at") or submission.get("workflow_state") == "graded")
    for key in ("submitted", "has_submitted_submissions"):
        if key in item:
            return bool(item[key])
    return False


def _iso(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return text or None


def _assignment_items(settings: Settings, warnings: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    folders = _course_folders(settings)
    mirror_dirs = sorted(
        d for d in settings.paths.canvas_mirror.glob("*") if d.is_dir() and d.name != "_meta"
    )
    for course_dir in mirror_dirs:
        course = folders.get(course_dir.name, course_dir.name)
        for raw in _read_meta(course_dir / "_meta" / "assignments.json", warnings):
            out.append(
                {
                    "course": course,
                    "title": str(raw.get("name") or raw.get("title") or "(untitled)"),
                    "due_at": _iso(raw.get("due_at")),
                    "type": "assignment",
                    "url": raw.get("html_url"),
                    "source": "canvas/assignments",
                    "submitted": _submitted(raw),
                }
            )
    return out


def _planner_items(settings: Settings, warnings: list[str]) -> list[dict[str, Any]]:
    path = settings.paths.canvas_mirror / "_meta" / "planner.json"
    if not path.exists():
        warnings.append("no planner.json yet — deadlines come from per-course assignments only")
        return []
    by_id = _course_by_canvas_id(settings)
    out: list[dict[str, Any]] = []
    for raw in _read_meta(path, warnings):
        plannable = raw.get("plannable") if isinstance(raw.get("plannable"), dict) else {}
        course_id = str(raw.get("course_id") or "")
        submissions = raw.get("submissions")
        submitted = bool(submissions.get("submitted")) if isinstance(submissions, dict) else False
        out.append(
            {
                "course": by_id.get(course_id, raw.get("context_name") or "(unknown course)"),
                "title": str(plannable.get("title") or raw.get("plannable_type") or "(untitled)"),
                "due_at": _iso(raw.get("plannable_date") or plannable.get("due_at")),
                "type": str(raw.get("plannable_type") or "item"),
                "url": raw.get("html_url"),
                "source": "canvas/planner",
                "submitted": submitted,
            }
        )
    return out


def _calendar_items(settings: Settings, report: DueReport) -> list[dict[str, Any]]:
    from . import calendar_read

    ok, detail = calendar_read.calendar_available(settings)
    if not ok:
        report.warnings.append(f"calendar unavailable: {detail}")
        return []

    start = datetime.now(UTC)
    end = start + timedelta(days=settings.calendar.lookahead_days)
    try:
        events = calendar_read.read_events(settings, start, end)
    except CalendarAccessDenied as exc:
        report.warnings.append(f"calendar access denied: {str(exc).splitlines()[0]}")
        return []
    except MitsyncError as exc:
        report.warnings.append(f"calendar unavailable: {exc}")
        return []
    report.calendar_ok = True
    out: list[dict[str, Any]] = []
    for event in events:
        if not event.course:
            continue
        out.append(
            {
                "course": event.course,
                "title": event.title,
                "due_at": event.start,
                "type": "event",
                "url": None,
                "source": "calendar",
                "submitted": False,
            }
        )
    return out


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Planner wins over assignments for the same thing; calendar is additive."""
    order = {"canvas/planner": 0, "canvas/assignments": 1, "calendar": 2}
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in sorted(items, key=lambda i: order.get(i["source"], 9)):
        key = (item["course"], item["title"].strip().lower(), item["due_at"] or "")
        if key in best:
            if item["submitted"]:
                best[key]["submitted"] = True
            continue
        best[key] = item
    return list(best.values())


def _sort_key(item: dict[str, Any]) -> tuple[int, str, str, str]:
    due = item.get("due_at") or ""
    return (1 if not due else 0, due, item.get("course") or "", item.get("title") or "")


def _last_sync(settings: Settings) -> str | None:
    db = settings.paths.manifest_db
    if not db.exists():
        return None
    from .manifest import Manifest

    with Manifest(db) as man:
        run = man.last_run("sync")
    return (run or {}).get("finished") or (run or {}).get("started")


def build_due(settings: Settings) -> DueReport:
    """Merge Canvas planner + assignments + calendar into ``_kb/due.json``."""
    report = DueReport()
    items = _planner_items(settings, report.warnings)
    items += _assignment_items(settings, report.warnings)
    items += _calendar_items(settings, report)
    report.items = sorted(_dedupe(items), key=_sort_key)
    report.warnings = list(dict.fromkeys(report.warnings))
    report.last_sync = _last_sync(settings)
    if report.last_sync is None:
        report.warnings.append("no successful `mitsync sync` recorded yet")

    path = settings.paths.kb / "due.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "last_sync": report.last_sync,
        "calendar_available": report.calendar_ok,
        "warnings": report.warnings,
        "items": report.items,
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report.path = path

    console.print(_due_table(report))
    for warning in report.warnings:
        console.print(f"[yellow]note:[/yellow] {warning}")
    console.print(f"wrote [bold]{path}[/bold]")
    return report


def _due_table(report: DueReport) -> Table:
    table = Table(title="due")
    for col in ("due", "course", "title", "type", "done"):
        table.add_column(col, overflow="fold")
    for item in report.items[:50]:
        table.add_row(
            item.get("due_at") or "(no date)",
            item.get("course") or "",
            item.get("title") or "",
            item.get("type") or "",
            "yes" if item.get("submitted") else "",
        )
    return table


# --------------------------------------------------------------------------
# briefing
# --------------------------------------------------------------------------
def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _previous_briefing(settings: Settings, today: Path) -> datetime | None:
    earlier = sorted(p for p in settings.paths.kb_briefings.glob("*.md") if p.name != today.name)
    if not earlier:
        return None
    return _parse(earlier[-1].stem)


def _recent_materials(settings: Settings, since: datetime | None) -> list[tuple[str, str, str]]:
    db = settings.paths.manifest_db
    if not db.exists():
        return []
    from .manifest import Manifest

    cutoff = since or datetime.now(UTC) - timedelta(days=BRIEFING_WINDOW_DAYS)
    out: list[tuple[str, str, str]] = []
    with Manifest(db) as man:
        for rec in man.list_files():
            seen = _parse(rec.first_seen) or _parse(rec.last_synced)
            if seen is None or seen < cutoff:
                continue
            out.append((rec.course_folder, rec.display_name or rec.filename, rec.filed_path or ""))
    return sorted(out)[:40]


def _class_meetings(settings: Settings, report: DueReport) -> list[Any]:
    from . import calendar_read

    ok, detail = calendar_read.calendar_available(settings)
    if not ok:
        report.warnings.append(f"calendar unavailable: {detail}")
        return []

    start = datetime.now(UTC)
    end = start + timedelta(days=BRIEFING_WINDOW_DAYS)
    try:
        return calendar_read.read_events(settings, start, end)
    except CalendarAccessDenied as exc:
        report.warnings.append(f"calendar access denied: {str(exc).splitlines()[0]}")
    except MitsyncError as exc:
        report.warnings.append(f"calendar unavailable: {exc}")
    return []


def write_briefing(settings: Settings) -> Path:
    """Write ``_kb/briefings/<YYYY-MM-DD>.md`` and return its path."""
    report = build_due(settings)
    now = datetime.now(UTC)
    horizon = now + timedelta(days=BRIEFING_WINDOW_DAYS)

    briefings = settings.paths.kb_briefings
    briefings.mkdir(parents=True, exist_ok=True)
    path = briefings / f"{now.strftime('%Y-%m-%d')}.md"
    previous = _previous_briefing(settings, path)

    upcoming = []
    undated = []
    for item in report.items:
        due = _parse(item.get("due_at"))
        if due is None:
            undated.append(item)
        elif now - timedelta(hours=12) <= due <= horizon:
            upcoming.append((due, item))

    events = _class_meetings(settings, report)
    report.warnings = list(dict.fromkeys(report.warnings))
    materials = _recent_materials(settings, previous)

    lines: list[str] = [
        f"# Briefing — {now.strftime('%A %d %B %Y')}",
        "",
        f"_Generated {now.isoformat(timespec='seconds')}._",
        "",
        "## Sync freshness",
        "",
    ]
    if report.last_sync:
        age = now - (_parse(report.last_sync) or now)
        hours = age.total_seconds() / 3600
        staleness = "fresh" if hours < 24 else f"**{hours / 24:.1f} days old**"
        lines.append(f"- Last successful sync: `{report.last_sync}` ({staleness}).")
    else:
        lines.append("- **No successful sync recorded yet** — run `mitsync sync`.")
    lines += ["", f"## Due in the next {BRIEFING_WINDOW_DAYS} days", ""]
    if upcoming:
        lines.append("| when | course | what | type | done |")
        lines.append("| --- | --- | --- | --- | --- |")
        for due, item in sorted(upcoming):
            lines.append(
                f"| {due.strftime('%a %d %b %H:%M')} | {item['course']} | {item['title']} "
                f"| {item['type']} | {'yes' if item['submitted'] else ''} |"
            )
    else:
        lines.append("_Nothing due in the window._")

    lines += ["", "## Classes and calendar", ""]
    if events:
        for event in events:
            tag = f" — _{event.course}_" if event.course else ""
            lines.append(f"- **{event.when}** {event.title}{tag}")
    elif report.calendar_ok:
        lines.append("_No events in the window._")
    else:
        lines.append(
            "_Calendar unavailable — class times are not in this briefing. "
            "See the notes at the end._"
        )

    since_text = previous.strftime("%Y-%m-%d") if previous else "the last week"
    lines += ["", f"## New material since {since_text}", ""]
    if materials:
        for course, name, filed in materials:
            where = f" → `{filed}`" if filed else " _(not filed yet)_"
            lines.append(f"- **{course}**: {name}{where}")
    else:
        lines.append("_Nothing new in the mirror._")

    if undated:
        lines += ["", "## Undated items", ""]
        lines += [f"- {i['course']}: {i['title']}" for i in undated[:20]]

    if report.warnings:
        lines += ["", "## Gaps in this briefing", ""]
        lines += [f"- {w}" for w in report.warnings]

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    console.print(f"wrote [bold]{path}[/bold]")
    return path

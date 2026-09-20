"""
# Deadlines

What is due, and the daily briefing that says so.

## 1. What This Module Does

Merges three sources into one answer: Canvas `planner/items` and per-course
assignments (both read from the `_meta/*.json` files `sync` already wrote),
and today's calendar events. `build_due` writes `_kb/due.json`;
`write_briefing` writes `_kb/briefings/<YYYY-MM-DD>.md`.

## 2. Why This Module Exists

"What do I actually have to do this week?" is the question the whole tool
exists to answer, and no single source can answer it: Canvas knows the
deadlines, the calendar knows when the class meets, and neither knows which
of them the student has already handled.

## 3. How It Fits in the Architecture

Pure derivation -- it reads state on disk and calls no network. It sits above
`config.read_meta`, `course_map` and `calendar_read`, and deliberately imports
neither `sync` nor `organize`, so reading local JSON does not drag in the
Canvas client or `httpx`.

## 4. Key Concepts

**Catch-up safety.** Nothing is computed from "time since the last run", so a
laptop that was shut for a week produces a correct briefing rather than
skipping the window it missed. The briefing reports its own staleness from the
manifest's last successful sync.

**Absent is not corrupt.** A missing calendar, a missing `_meta` file or an
empty manifest is a *reported gap*, carried in `DueReport.warnings` and shown
in the briefing. A `_meta` file that is not valid JSON, or a `courses.yml`
that does not parse, means something upstream wrote garbage and raises -- a
briefing that quietly omits half a term is worse than no briefing.

**Why an exception is caught here.** `CalendarAccessDenied` and a calendar
CLI failure become a warning, not an error, because an unavailable calendar is
a correct answer: the briefing still lists every Canvas deadline and says the
calendar could not be read. Inventing a class time would be the failure.

**Ties are the common case.** A whole course's assignments land at 23:59, so
anything that orders deadlines must break ties explicitly rather than falling
through to comparing the items themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

from mitsync.core.clock import now_iso, parse_iso
from mitsync.core.config import read_json, read_meta
from mitsync.core.errors import CalendarAccessDenied, MitsyncError
from mitsync.core.logging import get_logger
from mitsync.filing.course_map import load_course_map

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

log = get_logger(__name__)
console = Console()

__all__ = ["DueReport", "build_due", "last_sync", "write_briefing"]

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
def _course_folders(settings: Settings) -> dict[str, str]:
    """Mirror folder -> the student's folder name (falling back to itself)."""
    mapping: dict[str, str] = {}
    entries = load_course_map(settings)
    by_canvas_id = {str(e.get("canvas_id")): str(e.get("folder") or "") for e in entries}

    for meta in sorted(settings.paths.canvas_mirror.glob("*/_meta/courses.json")):
        mirror = meta.parent.parent.name
        course_id = str(read_json(meta).get("course_canvas_id") or "")
        mapping[mirror] = by_canvas_id.get(course_id, "") or mirror
    return mapping


def _course_by_canvas_id(settings: Settings) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in load_course_map(settings):
        if entry.get("canvas_id") is not None and entry.get("folder"):
            out[str(entry["canvas_id"])] = str(entry["folder"])
    for meta in sorted(settings.paths.canvas_mirror.glob("*/_meta/courses.json")):
        doc = read_json(meta)
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
        meta = course_dir / "_meta" / "assignments.json"
        if not meta.exists():
            warnings.append(f"missing {meta.name} ({meta}) — run `mitsync sync`")
            continue
        for raw in read_meta(meta):
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
    for raw in read_meta(path):
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
    from mitsync.schedule import calendar as calendar_read

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


def last_sync(settings: Settings) -> str | None:
    """When `mitsync sync` last finished, per the manifest `runs` table.

    The manifest is the authority: it records the run, not a side effect of one.
    Everything that reports sync freshness -- the briefing, `_kb/AGENTS.md` --
    reads it here so the two can never disagree.
    """
    db = settings.paths.manifest_db
    if not db.exists():
        return None
    from mitsync.canvas.manifest import Manifest

    with Manifest(db) as man:
        run = man.last_run("sync")
    if run is None:
        return None
    return run["finished"] or run["started"]


def build_due(settings: Settings) -> DueReport:
    """Merge Canvas planner + assignments + calendar into ``_kb/due.json``."""
    report = DueReport()
    items = _planner_items(settings, report.warnings)
    items += _assignment_items(settings, report.warnings)
    items += _calendar_items(settings, report)
    report.items = sorted(
        _dedupe(items),
        key=lambda i: (
            1 if not i.get("due_at") else 0,
            i.get("due_at") or "",
            i.get("course") or "",
            i.get("title") or "",
        ),
    )
    report.warnings = list(dict.fromkeys(report.warnings))
    report.last_sync = last_sync(settings)
    if report.last_sync is None:
        report.warnings.append("no successful `mitsync sync` recorded yet")

    path = settings.paths.kb / "due.json"
    document = {
        "generated_at": now_iso(),
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
    try:
        parsed = parse_iso(value)
    except ValueError:
        try:
            parsed = parse_iso(str(value).strip()[:10])
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _recent_materials(settings: Settings, since: datetime | None) -> list[tuple[str, str, str]]:
    db = settings.paths.manifest_db
    if not db.exists():
        return []
    from mitsync.canvas.manifest import Manifest

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
    from mitsync.schedule import calendar as calendar_read

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
    path = briefings / f"{now.strftime('%Y-%m-%d')}.md"
    earlier = sorted(p for p in briefings.glob("*.md") if p.name != path.name)
    previous = _parse(earlier[-1].stem) if earlier else None

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
        # Sort on the timestamp alone. A bare sorted() falls through to
        # comparing the dicts when two deadlines share a due time, which is
        # common (a course's items all land at 23:59) and raises TypeError.
        for due, item in sorted(upcoming, key=lambda p: (p[0], p[1]["course"], p[1]["title"])):
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

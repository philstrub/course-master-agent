"""
# Deadlines

What is due, what the student has already done about it, and when class meets
-- as plain data for the driving agent to reason over.

## 1. What This Module Does

Merges five sources into one answer: Canvas `planner/items` and per-course
assignments (both read from the `_meta/*.json` files `sync` already wrote),
calendar events, the Gradescope snapshot `gradescope sync` wrote, and the
required readings the agent recorded from each syllabus (`readings`).
`build_due` writes `state/due.json` and returns the same document. `homework`,
`work_evidence`, `class_meetings`, `submission_status` and `last_sync` are
the public data functions behind `mitsync due`, `mitsync work` and any skill
that imports the library directly. Every one of them
returns dicts and lists -- never markdown -- because deciding what the data
means, and how to say it, is the agent's job.

## 2. Why This Module Exists

"What do I actually have to do this week?" is the question the whole tool
exists to answer, and no single source can answer it: Canvas knows the
deadlines, the calendar knows when the class meets, and only the disk knows
which assignments the student has already started. This module gathers all
three facts deterministically; it does not interpret them.

## 3. How It Fits in the Architecture

Pure derivation -- it reads state on disk and calls no network. It sits above
`config.read_meta`, `course_map` and `calendar_read`, and deliberately imports
neither `sync` nor `organize`, so reading local JSON does not drag in the
Canvas client or `httpx`.

## 4. Key Concepts

**The student's own submission, never the course's.** `submission_status`
and `is_submitted` read the `submission` object Canvas attaches for the
current user. `has_submitted_submissions` is true as soon as *anyone* in the
course submits, so it is never consulted.

**Gradescope wins on status.** A course that collects work on Gradescope
leaves a `not_graded` placeholder on Canvas that reads `unsubmitted` forever.
When exactly one Canvas row of the same course has the Gradescope
assignment's `title_key`, that row takes Gradescope's status and its source
becomes `canvas/...+gradescope`. A Gradescope assignment with no Canvas row
is added as its own `gradescope` row, and one whose key matches several
Canvas rows is added on its own with a warning rather than guessed.

**Provenance tags.** `work_evidence` tags each file in a course folder as
`canvas_copy` (its bytes match a mirrored file), `edited` (it shares a mirrored
file's name but not its bytes) or `yours` (neither). Every
`assignments/<item>/` folder is listed in full; elsewhere only the student's
own files modified in the last `RECENT_WORK_DAYS` are, because homework often
lives in a folder the student made (`Assignment 1/`).

**Catch-up safety.** Nothing is computed from "time since the last run", so a
laptop that was shut for a week produces a correct answer rather than skipping
the window it missed. Staleness is reported from the manifest's last
successful sync.

**Absent is not corrupt.** A missing calendar, a missing `_meta` file or an
empty manifest is a *reported gap*, carried in the `warnings` list. A `_meta`
file that is not valid JSON, or a `courses.yml` that does not parse, means
something upstream wrote garbage and raises -- a deadline list that quietly
omits half a term is worse than none.

**Why an exception is caught here.** `CalendarAccessDenied` and a calendar
CLI failure become a warning in `class_meetings`, not an error, because an
unavailable calendar is a correct answer: every Canvas deadline is still
listed, with a note that the calendar could not be read. Inventing a class
time would be the failure.

**Ties are the common case.** A whole course's assignments land at 23:59, so
anything that orders deadlines must break ties explicitly rather than falling
through to comparing the items themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mitsync.core.clock import now_iso, parse_iso
from mitsync.core.config import read_json, read_meta
from mitsync.core.errors import CalendarAccessDenied, MitsyncError
from mitsync.core.logging import get_logger
from mitsync.filing.course_map import existing_course_folders, load_course_map
from mitsync.gradescope import snapshot as gs_snapshot
from mitsync.schedule.readings import reading_items

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

log = get_logger(__name__)

__all__ = [
    "BUILD_ARTIFACTS",
    "DEFAULT_WINDOW_DAYS",
    "FILES_PER_FOLDER",
    "RECENT_WORK_DAYS",
    "DueReport",
    "assignment_facts",
    "build_due",
    "class_meetings",
    "course_files",
    "homework",
    "in_window",
    "is_submitted",
    "last_sync",
    "plain_text",
    "submission_status",
    "work_evidence",
    "work_report",
]

DEFAULT_WINDOW_DAYS = 14
RECENT_WORK_DAYS = 14
DESCRIPTION_CHARS = 500
FILES_PER_FOLDER = 8  # newest first; a notebook export can leave dozens of files
BUILD_ARTIFACTS = (".aux", ".log", ".fls", ".fdb_latexmk", ".synctex.gz", ".out", ".toc")

#: Provenance tags `work_evidence` assigns, in the order a reader cares about.
TAG_CANVAS_COPY = "canvas_copy"
TAG_EDITED = "edited"
TAG_YOURS = "yours"


@dataclass
class DueReport:
    items: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    calendar_ok: bool = False
    last_sync: str | None = None
    path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": now_iso(),
            "last_sync": self.last_sync,
            "calendar_available": self.calendar_ok,
            "warnings": list(self.warnings),
            "items": list(self.items),
        }


# --------------------------------------------------------------------------
# small helpers
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


def _iso(value: Any) -> str | None:
    """A timestamp as ISO 8601 with an explicit offset, or the raw text if unparseable."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = _parse(text)
    return parsed.isoformat(timespec="seconds") if parsed else text


def plain_text(html: str, limit: int = DESCRIPTION_CHARS) -> str:
    """Canvas HTML reduced to one line of text, for an agent to read as data."""
    text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def in_window(item: dict[str, Any], now: datetime, days: int) -> bool:
    """Due between 12 hours ago and ``days`` from now (undated items are not)."""
    due = _parse(item.get("due_at"))
    return due is not None and now - timedelta(hours=12) <= due <= now + timedelta(days=days)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def is_submitted(item: dict[str, Any]) -> bool:
    """Whether *this student* has submitted a Canvas assignment record."""
    submission = item.get("submission")
    if isinstance(submission, dict):
        return bool(submission.get("submitted_at") or submission.get("workflow_state") == "graded")
    # Never `has_submitted_submissions`: it is true once *any* student submits.
    return bool(item.get("submitted"))


def submission_status(item: dict[str, Any]) -> str:
    """The student's own submission state, as Canvas reports it."""
    submission = item.get("submission")
    if not isinstance(submission, dict):
        return "unknown (re-run `mitsync sync`)"
    if submission.get("missing"):
        state = "missing"
    else:
        state = str(submission.get("workflow_state") or "unsubmitted")
        if submission.get("late"):
            state += " (late)"
    score, points = submission.get("score"), item.get("points_possible")
    if score is not None and points:
        state += f", {score:g}/{points:g}"
    return state


def _item(
    course: str,
    title: str,
    due_at: Any,
    kind: str,
    url: Any,
    source: str,
    submitted: bool,
    status: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """One deadline row. Every row carries every key, so the JSON shape is stable."""
    return {
        "course": course,
        "title": title,
        "due_at": _iso(due_at),
        "type": kind,
        "url": url,
        "source": source,
        "submitted": submitted,
        "status": status,
        "description": description,
    }


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
                _item(
                    course,
                    str(raw.get("name") or raw.get("title") or "(untitled)"),
                    raw.get("due_at"),
                    "assignment",
                    raw.get("html_url"),
                    "canvas/assignments",
                    is_submitted(raw),
                    status=submission_status(raw),
                    description=plain_text(str(raw.get("description") or "")) or None,
                )
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
            _item(
                by_id.get(course_id, raw.get("context_name") or "(unknown course)"),
                str(plannable.get("title") or raw.get("plannable_type") or "(untitled)"),
                raw.get("plannable_date") or plannable.get("due_at"),
                str(raw.get("plannable_type") or "item"),
                raw.get("html_url"),
                "canvas/planner",
                submitted,
            )
        )
    return out


def _calendar_items(settings: Settings, report: DueReport) -> list[dict[str, Any]]:
    meetings = class_meetings(settings, settings.calendar.lookahead_days)
    report.warnings += meetings["warnings"]
    report.calendar_ok = meetings["available"]
    return [
        _item(e["course"], e["title"], e["start"], "event", None, "calendar", False)
        for e in meetings["events"]
        if e.get("course")
    ]


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Planner wins over assignments for the same thing; calendar is additive.

    The assignment record still contributes what only it knows -- the
    student's own status and the description -- to the planner row it loses to.
    """
    order = {"canvas/planner": 0, "canvas/assignments": 1, "calendar": 2}
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in sorted(items, key=lambda i: order.get(i["source"], 9)):
        key = (item["course"], item["title"].strip().lower(), item["due_at"] or "")
        if key in best:
            kept = best[key]
            if item["submitted"]:
                kept["submitted"] = True
            for detail in ("status", "description"):
                if kept.get(detail) is None and item.get(detail) is not None:
                    kept[detail] = item[detail]
            continue
        best[key] = item
    return list(best.values())


_GS_DONE = frozenset({"submitted", "late", "graded"})
#: planner rows that are class sessions or release notices ("HW3 OUT"), never work
_NOT_WORK = frozenset({"calendar_event", "announcement", "event"})


def _gradescope_overlay(
    settings: Settings, items: list[dict[str, Any]], warnings: list[str]
) -> list[dict[str, Any]]:
    snap = gs_snapshot.load(settings)
    if snap is None:
        if os.environ.get("GRADESCOPE_COOKIE"):
            warnings.append("GRADESCOPE_COOKIE is set but `mitsync gradescope sync` never ran")
        return items
    added: list[dict[str, Any]] = []
    for course in snap.courses:
        if course.folder is None:
            continue
        rows = [
            i
            for i in items
            if i["course"] == course.folder
            and i["source"].startswith("canvas/")
            and i["type"] not in _NOT_WORK
        ]
        for a in course.assignments:
            key = gs_snapshot.title_key(a.title)
            hits = [r for r in rows if gs_snapshot.title_key(r["title"]) == key]
            if len(hits) == 1:
                row = hits[0]
                row["status"] = gs_snapshot.status_text(a)
                row["submitted"] = a.status in _GS_DONE
                row["source"] += "+gradescope"
                row["due_at"] = row["due_at"] or (a.due_at and _iso(a.due_at.isoformat()))
                continue
            if hits:
                warnings.append(
                    f"gradescope {course.folder} {a.title!r} matches {len(hits)} Canvas items; "
                    "listed on its own"
                )
            added.append(
                _item(
                    course.folder, a.title, a.due_at and a.due_at.isoformat(), "assignment",
                    a.url, "gradescope", a.status in _GS_DONE, status=gs_snapshot.status_text(a),
                )
            )  # fmt: skip
    return items + added


_PLACEHOLDER_TYPES = frozenset({"none", "not_graded", "on_paper", "external_tool"})
_KIND_OF_KEY = {"hw": "homework", "lab": "homework", "midterm": "exam", "exam": "exam",
                "quiz": "quiz", "project": "project"}  # fmt: skip


def _kind(title: str) -> str | None:
    key = gs_snapshot.title_key(title)
    return _KIND_OF_KEY.get(key[0]) if key else None


def _canvas_status(raw: dict[str, Any]) -> str | None:
    """Canvas's status in the ontology's words; None for a placeholder that takes no work."""
    if set(raw.get("submission_types") or []) <= _PLACEHOLDER_TYPES:
        return None  # handed in elsewhere (Gradescope, paper): Canvas cannot know
    sub = raw.get("submission") or {}
    if sub.get("missing"):
        return "missing"
    state = sub.get("workflow_state") or "unsubmitted"
    if state == "graded":
        return "graded"
    if state in ("submitted", "pending_review"):
        return "late" if sub.get("late") else "submitted"
    return "unsubmitted"


def assignment_facts(settings: Settings) -> list[dict[str, Any]]:
    """Every Canvas and Gradescope assignment of a mapped course, as `Assignment` attrs.

    Each dict carries `course` (the student's folder), `src` and `ts` next to the
    attrs. Gradescope's status and score win on the Canvas record with the same
    `title_key`, exactly as in `due`, and a Gradescope-only assignment is its own
    fact. This is the graph's memory of submitted work.
    """
    folders = _course_folders(settings)
    mapped = set(existing_course_folders(settings))
    facts: list[dict[str, Any]] = []
    for meta in sorted(settings.paths.canvas_mirror.glob("*/_meta/assignments.json")):
        course = folders.get(meta.parent.parent.name)
        if course not in mapped:
            continue
        fetched = read_json(meta).get("fetched_at")
        for raw in read_meta(meta):
            sub = raw.get("submission") or {}
            title = str(raw.get("name") or "(untitled)")
            status = _canvas_status(raw)
            facts.append({
                "course": course, "title": title,
                "kind": _kind(title),
                "due_at": _iso(raw.get("due_at")), "canvas_id": raw.get("id"),
                "points_possible": raw.get("points_possible"),
                "submission_status": status,
                "submitted_at": _iso(sub.get("submitted_at")) if status else None,
                "score": sub.get("score") if status else None,
                "submitted_via": "canvas" if status else None,
                "src": [settings.paths.safe_relative(meta).as_posix()], "ts": _iso(fetched),
            })  # fmt: skip
    snap = gs_snapshot.load(settings)
    for course in snap.courses if snap else []:
        if course.folder not in mapped:
            continue
        rows = [f for f in facts if f["course"] == course.folder]
        for a in course.assignments:
            key = gs_snapshot.title_key(a.title)
            hits = [f for f in rows if gs_snapshot.title_key(f["title"]) == key]
            fact = hits[0] if len(hits) == 1 else None
            if fact is None:
                fact = {"course": course.folder, "title": a.title,
                        "kind": _kind(a.title),
                        "due_at": a.due_at and a.due_at.isoformat(), "src": []}  # fmt: skip
                facts.append(fact)
            fact |= {
                "submission_status": a.status, "score": a.score, "submitted_via": "gradescope",
                "points_possible": a.points or fact.get("points_possible"),
                "src": [*fact["src"],
                        a.url or f"https://www.gradescope.com/courses/{course.gradescope_id}"],
                "ts": snap.fetched_at.isoformat(),
            }  # fmt: skip
    return facts


def last_sync(settings: Settings) -> str | None:
    """When `mitsync sync` last finished, per the manifest `runs` table.

    The manifest is the authority: it records the run, not a side effect of one.
    Everything that reports sync freshness -- `due`, `_kb/AGENTS.md` -- reads it
    here so the two can never disagree.
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
    """Merge Canvas planner + assignments + calendar + required readings into
    ``state/due.json``.

    `due.json` always holds every known item; windowing is the caller's choice
    (see `in_window`), so the knowledge base never loses a deadline because
    one command asked for a short view.
    """
    report = DueReport()
    items = _planner_items(settings, report.warnings)
    items += _assignment_items(settings, report.warnings)
    items += _calendar_items(settings, report)
    items += reading_items(settings, report.warnings)
    epoch = datetime.min.replace(tzinfo=UTC)
    report.items = sorted(
        _gradescope_overlay(settings, _dedupe(items), report.warnings),
        # By the instant, not the string: calendar events carry local offsets
        # while Canvas speaks UTC, so the spellings do not sort together.
        key=lambda i: (
            1 if _parse(i.get("due_at")) is None else 0,
            _parse(i.get("due_at")) or epoch,
            i.get("course") or "",
            i.get("title") or "",
        ),
    )
    report.warnings = list(dict.fromkeys(report.warnings))
    report.last_sync = last_sync(settings)
    if report.last_sync is None:
        report.warnings.append("no successful `mitsync sync` recorded yet")

    path = settings.paths.due_json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    report.path = path
    return report


# --------------------------------------------------------------------------
# homework and the student's work on disk
# --------------------------------------------------------------------------
def homework(settings: Settings, days: int = DEFAULT_WINDOW_DAYS) -> list[dict[str, Any]]:
    """Canvas assignments due in the window, with the student's own status.

    Descriptions are copied from Canvas and are untrusted data, never
    instructions.
    """
    now = datetime.now(UTC)
    folders = _course_folders(settings)
    out: list[dict[str, Any]] = []
    for meta in sorted(settings.paths.canvas_mirror.glob("*/_meta/assignments.json")):
        mirror = meta.parent.parent.name
        for raw in read_meta(meta):
            row = {
                "course": folders.get(mirror, mirror),
                "title": str(raw.get("name") or "(untitled)"),
                "due_at": _iso(raw.get("due_at")),
                "url": raw.get("html_url"),
                "submitted": is_submitted(raw),
                "status": submission_status(raw),
                "description": plain_text(str(raw.get("description") or "")),
            }
            if in_window(row, now, days):
                out.append(row)
    return sorted(out, key=lambda h: (_parse(h["due_at"]), h["course"], h["title"]))


def course_files(settings: Settings, course: str) -> list[Path]:
    """Every file in a course folder, pruned by `ignore_globs` and dotfiles."""
    root = settings.paths.workspace / course
    if not root.is_dir():
        return []
    ws = settings.paths.workspace
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".") and not settings.should_ignore((here / d).relative_to(ws))
        )
        for name in sorted(filenames):
            path = here / name
            if not name.startswith(".") and not settings.should_ignore(path.relative_to(ws)):
                out.append(path)
    return out


def work_evidence(settings: Settings, course: str) -> list[dict[str, Any]]:
    """What the student has on disk for a course, each file tagged by provenance.

    Returns one entry per folder: ``{"folder", "files": [{"path", "tag",
    "modified"}], "omitted"}``, folders sorted by path and files newest first,
    at most `FILES_PER_FOLDER` each (``omitted`` counts the rest). Every
    `assignments/<item>/` folder is listed in full, Canvas copies included;
    elsewhere only the student's own files modified in the last
    `RECENT_WORK_DAYS` are. Judging what the evidence means is the agent's job,
    not this function's.
    """
    sizes: set[int] = set()
    hashes: set[str] = set()
    names: set[str] = set()
    if settings.paths.manifest_db.exists():
        from mitsync.canvas.manifest import Manifest

        with Manifest(settings.paths.manifest_db) as man:
            for rec in man.list_files():
                hashes.add(rec.sha256)
                names.update({rec.filename, rec.display_name})
                if rec.size is not None:
                    sizes.add(rec.size)

    def tag(path: Path, size: int) -> str:
        # Hash only on a size match: a course folder can hold a 100 MB textbook.
        if size in sizes and _sha256(path) in hashes:
            return TAG_CANVAS_COPY
        if path.name in names:
            return TAG_EDITED
        return TAG_YOURS

    ws = settings.paths.workspace
    cutoff = datetime.now().timestamp() - RECENT_WORK_DAYS * 86400
    groups: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for path in course_files(settings, course):
        if path.name.endswith(BUILD_ARTIFACTS):
            continue
        rel = path.relative_to(ws)
        stat = path.stat()
        in_assignment = len(rel.parts) > 3 and rel.parts[1] == "assignments"
        if not in_assignment and stat.st_mtime < cutoff:
            continue
        label = tag(path, stat.st_size)
        if not in_assignment and label == TAG_CANVAS_COPY:
            continue
        folder = rel.parent.as_posix() if not in_assignment else "/".join(rel.parts[:3])
        row = {
            "path": rel.as_posix(),
            "tag": label,
            "modified": datetime.fromtimestamp(stat.st_mtime, UTC)
            .astimezone()
            .isoformat(timespec="seconds"),
        }
        groups.setdefault(folder, []).append((stat.st_mtime, row))
    out: list[dict[str, Any]] = []
    for folder, dated in sorted(groups.items()):
        rows = [row for _, row in sorted(dated, key=lambda p: (-p[0], p[1]["path"]))]
        out.append(
            {
                "folder": folder,
                "files": rows[:FILES_PER_FOLDER],
                "omitted": max(0, len(rows) - FILES_PER_FOLDER),
            }
        )
    return out


def work_report(settings: Settings, course: str | None = None) -> dict[str, Any]:
    """`work_evidence` for one course or every mapped course, as one document."""
    known = existing_course_folders(settings)
    if course is not None and course not in known:
        raise MitsyncError(
            f"no course folder named {course!r}; mapped folders on disk: "
            + (", ".join(known) or "none (see config/courses.yml)")
        )
    courses = [course] if course is not None else known
    out = [{"course": name, "folders": work_evidence(settings, name)} for name in courses]
    return {
        "generated_at": now_iso(),
        "recent_days": RECENT_WORK_DAYS,
        "files_per_folder": FILES_PER_FOLDER,
        "tags": [TAG_CANVAS_COPY, TAG_EDITED, TAG_YOURS],
        "courses": out,
    }


# --------------------------------------------------------------------------
# class meetings
# --------------------------------------------------------------------------
def class_meetings(settings: Settings, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Calendar events from now to ``days`` ahead, as data.

    Returns ``{"available": bool, "events": [...], "warnings": [...]}``. An
    unavailable or denied calendar is ``available: False`` plus a warning, never
    an exception and never an invented event.
    """
    from mitsync.schedule import calendar as calendar_read

    ok, detail = calendar_read.calendar_available(settings)
    if not ok:
        return {"available": False, "events": [], "warnings": [f"calendar unavailable: {detail}"]}

    start = datetime.now(UTC)
    end = start + timedelta(days=days)
    try:
        events = calendar_read.read_events(settings, start, end)
    except CalendarAccessDenied as exc:
        warning = f"calendar access denied: {str(exc).splitlines()[0]}"
        return {"available": False, "events": [], "warnings": [warning]}
    except MitsyncError as exc:
        return {"available": False, "events": [], "warnings": [f"calendar unavailable: {exc}"]}
    return {"available": True, "events": [e.to_dict() for e in events], "warnings": []}

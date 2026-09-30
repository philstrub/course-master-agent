"""
# Gradescope Client

GET the student's Gradescope pages with their session cookie and turn them
into a `Snapshot`.

## 1. What This Module Does

`GradescopeClient` sends GET requests to gradescope.com with the cookie from
`GRADESCOPE_COOKIE`. `parse_courses` reads the account page's course boxes,
`parse_assignments` reads a course page's student assignment table, and
`sync` fetches both for every course `config/courses.yml` maps and writes
`state/gradescope.json`.

## 2. Why This Module Exists

Gradescope has no student API. The dashboard HTML is stable enough to parse
and carries exactly what is missing from Canvas: per assignment, the
student's submission state, score and due dates.

## 3. How It Fits in the Architecture

The second HTTP client after `canvas.client`, with the same chokepoint rule:
`_request` refuses every method outside `READ_ONLY_METHODS`, and
`tests/test_canvas_read_only.py` scans this package too. Gradescope holds
graded work exactly like Canvas does.

## 4. Key Concepts

**The cookie is the credential.** MIT signs in to Gradescope through
Touchstone, so there is no password to send. The student copies the `Cookie`
request header of any logged-in gradescope.com page (browser devtools >
Network) into `GRADESCOPE_COOKIE` in `.env`. When it expires Gradescope
redirects to `/login`, and `_request` raises `GradescopeAuthError` saying so.

**Course mapping.** A Gradescope course belongs to a workspace folder when
`courses.yml` gives that entry a matching `gradescope_id`, or else when the
course box's short name ("15.C57") is the entry's `course_number` or one of
its `aliases`. Unmapped courses are kept in the snapshot with `folder: null`
and not fetched.

**Parsing is strict.** A course page without the student assignment table,
and without Gradescope's empty-course notice, raises: the layout changed and a
silently empty course would read as "nothing due".
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from bs4 import BeautifulSoup, Tag

from mitsync.core.errors import GradescopeAuthError, GradescopeWriteRefused, MitsyncError
from mitsync.core.logging import get_logger
from mitsync.filing.course_map import load_course_map
from mitsync.gradescope import snapshot
from mitsync.gradescope.snapshot import GsAssignment, GsCourse, Snapshot

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

log = get_logger(__name__)

__all__ = ["GradescopeClient", "parse_assignments", "parse_courses", "sync"]

BASE_URL = "https://www.gradescope.com"
COOKIE_ENV = "GRADESCOPE_COOKIE"
READ_ONLY_METHODS = frozenset({"GET", "HEAD"})
_SCORE_RX = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
_EMPTY_COURSE = "You don't have any assignments yet"


class GradescopeClient:
    def __init__(self, cookie: str | None = None, transport: httpx.BaseTransport | None = None):
        cookie = cookie or os.environ.get(COOKIE_ENV)
        if not cookie:
            raise GradescopeAuthError(
                f"{COOKIE_ENV} is not set: copy the Cookie request header of a logged-in "
                "gradescope.com page (devtools > Network) into _agent/.env"
            )
        self._client = httpx.Client(
            base_url=BASE_URL,
            transport=transport,
            timeout=30.0,
            follow_redirects=False,
            headers={"Cookie": cookie, "Accept": "text/html"},
        )

    def __enter__(self) -> GradescopeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _request(self, method: str, url: str) -> httpx.Response:
        # Read-only by requirement: Gradescope holds graded work, like Canvas.
        if method.upper() not in READ_ONLY_METHODS:
            raise GradescopeWriteRefused(method, url)
        response = self._client.request(method, url)
        if response.is_redirect and "/login" in response.headers.get("location", ""):
            raise GradescopeAuthError(
                f"Gradescope redirected {url} to /login: {COOKIE_ENV} has expired. "
                "Copy a fresh Cookie header from a logged-in gradescope.com page into .env"
            )
        response.raise_for_status()
        return response

    def page(self, url: str) -> str:
        return self._request("GET", url).text


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def _text(tag: Tag | None) -> str:
    return " ".join(tag.get_text(" ").split()) if tag else ""


def parse_courses(html: str) -> list[dict[str, Any]]:
    """Course boxes on the account page: id, shortname, name, term."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for box in soup.select("a.courseBox[href^='/courses/']"):
        term = box.find_parent(class_="courseList--coursesForTerm")
        heading = term.find_previous_sibling(class_="courseList--term") if term else None
        out.append(
            {
                "gradescope_id": box["href"].rstrip("/").split("/")[-1],
                "shortname": _text(box.select_one(".courseBox--shortname")),
                "name": _text(box.select_one(".courseBox--name")),
                "term": _text(heading) or None,
            }
        )
    if not out and "courseList" not in html:
        raise MitsyncError("Gradescope account page has no course list: the layout changed")
    return out


def _when(cell: Tag, cls: str) -> list[datetime]:
    return [
        datetime.strptime(t["datetime"], "%Y-%m-%d %H:%M:%S %z").astimezone(UTC)
        for t in cell.select(f"time.{cls}[datetime]")
    ]


def parse_assignments(html: str, now: datetime) -> list[GsAssignment]:
    """Rows of the student assignment table on a course page."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#assignments-student-table")
    if table is None:
        if _EMPTY_COURSE in html:
            return []
        raise MitsyncError("Gradescope course page has no student assignment table")
    out: list[GsAssignment] = []
    for row in table.select("tbody tr"):
        head = row.select_one("th")
        link = head.select_one("a[href]") if head else None
        button = head.select_one("[data-assignment-id]") if head else None
        ident = (
            re.search(r"/assignments/(\d+)", link["href"]).group(1)
            if link
            else (button["data-assignment-id"] if button else None)
        )
        status_cell = row.select_one(".submissionStatus")
        status_raw = _text(status_cell).lower()
        dues = _when(row, "submissionTimeChart--dueDate")
        released = _when(row, "submissionTimeChart--releaseDate")
        score = _SCORE_RX.search(status_raw)
        if score:
            status = "graded"
        elif "no submission" in status_raw:
            status = "missing" if dues and dues[-1] < now else "unsubmitted"
        elif "late" in status_raw:
            status = "late"
        elif "submitted" in status_raw:
            status = "submitted"
        else:
            raise MitsyncError(f"Gradescope row {_text(head)!r}: unknown status {status_raw!r}")
        out.append(
            GsAssignment(
                gradescope_id=ident,
                title=_text(head),
                url=f"{BASE_URL}{link['href']}" if link else None,
                released_at=released[0] if released else None,
                due_at=dues[0] if dues else None,
                late_due_at=dues[1] if len(dues) > 1 else None,
                status=status,
                score=float(score.group(1)) if score else None,
                points=float(score.group(2)) if score else None,
            )
        )
    return out


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------
def _folder_for(course: dict[str, Any], entries: list[dict[str, Any]]) -> str | None:
    for entry in entries:
        if str(entry.get("gradescope_id") or "") == course["gradescope_id"]:
            return entry["folder"]
    short = course["shortname"].lower()
    for entry in entries:
        names = [entry.get("course_number"), *(entry.get("aliases") or [])]
        if any(str(n).lower() == short for n in names if n):
            return entry["folder"]
    return None


def sync(settings: Settings, client: GradescopeClient | None = None) -> Snapshot:
    """Fetch every mapped course's assignments and write `state/gradescope.json`."""
    entries = load_course_map(settings)
    now = datetime.now(UTC)
    with client or GradescopeClient() as gs:
        courses = [
            GsCourse(**c, folder=_folder_for(c, entries)) for c in parse_courses(gs.page("/"))
        ]
        for course in courses:
            if course.folder is not None:
                html = gs.page(f"/courses/{course.gradescope_id}")
                course.assignments = parse_assignments(html, now)
    snap = Snapshot(fetched_at=now, courses=courses)
    out = snapshot.path(settings)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(snap.model_dump_json(indent=2) + "\n", encoding="utf-8")
    log.info("gradescope sync: %d courses, %d mapped", len(courses),
             sum(c.folder is not None for c in courses))  # fmt: skip
    return snap

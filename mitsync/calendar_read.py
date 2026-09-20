"""
# Calendar (Read-Only)

Apple Calendar events for the student's courses, read through an EventKit CLI
and never written to.

## 1. What This Module Does

Shells out to an EventKit command-line tool (`ical-guy` by default; `ekctl`'s
JSON shape is also understood), asks it for the events in a date range,
normalises the reply into `Event` value objects, and tags each one with the
course folder it belongs to using `config/courses.yml`.

## 2. Why This Module Exists

Canvas knows what is due; only the calendar knows when class actually meets.
The briefing needs both. Reading the calendar out of process, through a CLI,
also keeps EventKit -- and the macOS-only dependency it brings -- out of the
rest of the tool.

mitsync never mutates the student's calendar. Asking for a query sub-command is
the only thing that appears anywhere in this file and the only thing that ever
will. There is deliberately no function here that stores an event, and the test
suite asserts structurally that no argv token in this module could be mistaken
for one. If you are tempted to add one: don't. It is the guardrail, not an
oversight.

## 3. How It Fits in the Architecture

`deadlines` is the only consumer, and it treats this module as best-effort:
an unavailable calendar is a reported gap in the briefing, never a failed run.
Course tagging is read from `course_map`, so this module never imports the
filing or Canvas machinery.

## 4. Key Concepts

**TCC is granted to the terminal, interactively, once.** macOS calendar access
is gated by TCC, and the grant belongs to the *terminal application* that runs
the binary. A background or launchd job can never be prompted -- it just gets
an access error forever -- so a denial raises `CalendarAccessDenied` carrying
the exact command to run by hand, rather than pretending the calendar is empty.

**The exact command line, and why each part of it.** For `ical-guy`:

    ical-guy events list --from YYYY-MM-DD --to YYYY-MM-DD
        --format json --group-by none

Verified against **ical-guy 0.13.0** on 2026-09-20 (`ical-guy events list
--help` plus the installed man page, `ical-guy.1`, dated 2026-04-13).
`events list` is a *sub-command pair*: `events` alone is the parent group and
`list` its default child. There is no `--json` flag -- the format is selected
with `--format json`, which also pins the output when stdout is a TTY.
`--group-by none` is passed explicitly because the grouping mode changes the
JSON *shape* (`date` and `calendar` wrap events in group objects) and because
it can otherwise be set behind our back by the user's
`~/.config/ical-guy/config.toml`.

**Dates are plain local calendar dates.** Two bugs, both found the hard way
against 0.13.0. It rejects a full ISO 8601 timestamp (`Invalid date format:
'2026-09-20T18:00:19.811774+00:00'`) and asks for `YYYY-MM-DD`. And the date
must be the *local* one: `datetime.now(UTC)` at 20:00 in America/New_York is
already the next day in UTC, so formatting the UTC date would silently shift
the whole window forward by a day every evening. `--from`/`--to` are inclusive
whole days, which is the right granularity for a lookahead window anyway.

**The documented event object** (man page section OUTPUT > JSON Output,
confirmed against live output) carries `id`, `title`, `startDate`, `endDate`,
`isAllDay`, `location`, `notes`, `url`, `meetingUrl`, `meetingVendor`,
`calendar` (an object with `id`/`title`/`type`/`source`/`color`), `attendees`,
`organizer`, `recurrence`, `status`, `availability`, `timeZone`,
`creationDate` and `lastModifiedDate`. Only a handful are read; the rest are
ignored.

**Why exceptions are caught here.** A third-party binary is a genuinely
external failure, and this is that boundary. Each handler translates one
foreign failure into an actionable message:

1. Missing binary (`shutil.which` miss, or a `FileNotFoundError` from a race)
   -- a warning plus an empty list, with install instructions. The calendar is
   optional; the rest of the tool still works.
2. A denial, matched on the exit status and stderr wording -- raised as
   `CalendarAccessDenied`, because a silent empty list would hide a fixable
   permission problem forever.
3. `subprocess.TimeoutExpired` -- EventKit can stall when a permission prompt
   is waiting on a window nobody can see; never hang a sync behind it.
4. Malformed JSON from the CLI -- raised with the first 200 characters quoted,
   so the operator can see what arrived instead.
5. A non-zero exit that is not a denial -- the CLI's own stderr, verbatim. If a
   future ical-guy changes this interface the symptom is an exit-64 usage
   error, and the fix is to re-run the three `--help` commands and update
   `_QUERY_ARGV`.

The multi-spelling key lookups (`_START_KEYS` and friends) are the same
instinct applied to data rather than control flow: a CLI that renames a field
should degrade to a missing value, not a `KeyError` in the middle of a run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from rich.console import Console

from .clock import parse_iso
from .course_map import load_course_map
from .errors import CalendarAccessDenied, MitsyncError
from .logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings

log = get_logger(__name__)
console = Console()

__all__ = ["Event", "calendar_available", "read_events", "tag_course"]

#: Never hang a sync behind a stalled EventKit query.
TIMEOUT_SECONDS = 25

#: The read-only sub-command for each supported CLI, keyed by binary name.
#: One adapter per tool, deliberately kept side by side rather than merged:
#: they disagree about flag spelling and about how the format is selected.
#: Nothing but a query sub-command ever appears here -- see the module
#: docstring, and the structural tests that enforce it.
_QUERY_ARGV: dict[str, list[str]] = {
    # ical-guy 0.13.0, verified 2026-09-20.
    "ical-guy": [
        "events",
        "list",
        "--from",
        "{start}",
        "--to",
        "{end}",
        "--format",
        "json",
        "--group-by",
        "none",
    ],
    # ekctl: flat `events` query with its own flag spelling.
    "ekctl": ["events", "--start", "{start}", "--end", "{end}", "--json"],
}
_DEFAULT_ARGV = _QUERY_ARGV["ical-guy"]

_DENIED_RX = re.compile(
    r"(denied|not\s+authoriz|unauthoriz|no\s+access|permission|tcc|restricted|"
    r"grant|privacy)",
    re.I,
)
_MISSING_HINT = (
    "Install it with `brew install ical-guy` (or point calendar.cli in "
    "config/settings.yml at an EventKit CLI you already have)."
)
_TCC_HINT = (
    "macOS denied calendar access.\n"
    "Grant it ONCE, INTERACTIVELY, from the same terminal app you run mitsync in:\n"
    "  1. run `{cli} {probe}` by hand in that terminal\n"
    "  2. click Allow on the system prompt\n"
    "  3. confirm the terminal app is checked under\n"
    "     System Settings > Privacy & Security > Calendars\n"
    "A background or launchd process can never show that prompt: it fails "
    "silently forever until the grant exists, so do this by hand first."
)


@dataclass
class Event:
    """One calendar event, normalised across CLIs. Purely a value object."""

    start: str
    end: str
    title: str
    calendar: str
    location: str | None = None
    notes: str | None = None
    all_day: bool = False
    course: str | None = None

    # -- dict-ish access, so a generic table renderer can treat us as a row ----
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["when"] = self.when
        return data

    @property
    def when(self) -> str:
        moment = _parse_dt(self.start)
        if moment is None:
            return self.start
        return moment.strftime("%a %d %b") if self.all_day else moment.strftime("%a %d %b %H:%M")

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self) -> Any:
        return self.to_dict().keys()

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __str__(self) -> str:  # so `--json` output stays readable
        return json.dumps(self.to_dict(), ensure_ascii=False)


def calendar_available(settings: Settings) -> tuple[bool, str]:
    """``(ok, explanation)`` -- is the configured EventKit CLI usable?"""
    cli = settings.calendar.cli
    found = shutil.which(cli)
    if not found:
        return False, f"'{cli}' is not on PATH. {_MISSING_HINT}"
    return True, f"{cli} at {found}"


def _day(moment: datetime) -> str:
    """``moment`` as a plain ``YYYY-MM-DD`` *local* calendar date.

    Two lessons, both learned the hard way against ical-guy 0.13.0:

    * It rejects a full ISO 8601 timestamp -- ``Invalid date format:
      '2026-09-20T18:00:19.811774+00:00'`` -- and asks for ``YYYY-MM-DD``,
      ``today``, ``today+N`` or a natural-language phrase. So we send a date.
    * The date must be the *local* one. ``datetime.now(UTC)`` at 20:00 in
      America/New_York is already the next day in UTC, so formatting the UTC
      date would silently shift the whole window by a day every evening.

    ``--from``/``--to`` are inclusive whole days (the man page: ``--to``
    "defaults to the end of the start date"), which is the right granularity
    for a lookahead window anyway.
    """
    return moment.astimezone().strftime("%Y-%m-%d")


def _template(cli: str) -> list[str]:
    """The query argv template for ``cli``, chosen by its *binary name*."""
    return _QUERY_ARGV.get(cli.rsplit("/", 1)[-1], _DEFAULT_ARGV)


def _argv(cli: str, start: datetime, end: datetime) -> list[str]:
    template = _template(cli)
    fmt = {"start": _day(start), "end": _day(end)}
    return [cli, *[part.format(**fmt) for part in template]]


def _tcc_message(cli: str) -> str:
    """The TCC remediation text, quoting the exact command to run by hand."""
    probe = " ".join(part.format(start="today", end="today") for part in _template(cli))
    return _TCC_HINT.format(cli=cli, probe=probe)


def read_events(
    settings: Settings, start: datetime | None = None, end: datetime | None = None
) -> list[Event]:
    """Events between ``start`` and ``end`` (defaults: now .. lookahead_days).

    Returns ``[]`` (after an actionable message) when the CLI is not installed;
    raises :class:`CalendarAccessDenied` when macOS refused access, because a
    silent empty list would hide a fixable permission problem.
    """
    start = start or datetime.now(UTC)
    end = end or start + timedelta(days=settings.calendar.lookahead_days)
    cli = settings.calendar.cli

    ok, detail = calendar_available(settings)
    if not ok:
        log.warning("calendar unavailable: %s", detail)
        console.print(f"[yellow]Calendar unavailable:[/yellow] {detail}")
        return []

    argv = _argv(cli, start, end)
    log.debug("calendar query: %s", " ".join(argv))
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        console.print(f"[yellow]Calendar unavailable:[/yellow] '{cli}' vanished. {_MISSING_HINT}")
        return []
    except subprocess.TimeoutExpired as exc:
        raise MitsyncError(
            f"'{cli}' did not answer within {TIMEOUT_SECONDS}s. EventKit can stall when the "
            f"permission prompt is waiting on a window you cannot see; run it by hand once."
        ) from exc
    except OSError as exc:
        raise MitsyncError(f"could not run '{cli}': {exc}") from exc

    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        if _DENIED_RX.search(stderr) or not stderr:
            tail = f"\n\n{stderr}" if stderr else ""
            raise CalendarAccessDenied(_tcc_message(cli) + tail)
        raise MitsyncError(f"'{cli}' failed (exit {proc.returncode}): {stderr}")
    if _DENIED_RX.search(stderr) and not (proc.stdout or "").strip():
        raise CalendarAccessDenied(_tcc_message(cli) + f"\n\n{stderr}")

    events = [_to_event(raw) for raw in _iter_raw(proc.stdout, cli)]
    tagger = _course_tagger(settings)
    for event in events:
        event.course = tagger(event)
    events.sort(key=lambda e: (e.start or "", e.title or ""))
    return events


def _iter_raw(stdout: str, cli: str) -> list[dict[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MitsyncError(
            f"'{cli}' did not emit JSON: {exc}; first 200 chars: {text[:200]!r}"
        ) from exc
    doc = _unwrap(doc)
    if not isinstance(doc, list):
        raise MitsyncError(f"'{cli}' JSON was neither a list nor an object of events")

    # `--group-by none` yields a flat array, but a stray config file or a
    # future default could still hand us group objects ({"date": ..., "events":
    # [...]}). Flatten one level of those rather than mis-reading a group as an
    # event; anything else that is not a dict is quietly skipped.
    out: list[dict[str, Any]] = []
    for item in doc:
        if isinstance(item, dict):
            out.extend(d for d in _unwrap(item) if isinstance(d, dict))
    return out


#: Keys under which a wrapper object may hide the actual list of events.
_LIST_KEYS = ("events", "items", "results", "data")


def _unwrap(doc: Any) -> Any:
    """A wrapper object's event list, or ``doc`` unchanged."""
    if isinstance(doc, dict):
        for key in _LIST_KEYS:
            if isinstance(doc.get(key), list):
                return doc[key]
        return [doc]
    return doc


_START_KEYS = ("start", "startDate", "start_date", "startsAt", "starts_at", "begin")
_END_KEYS = ("end", "endDate", "end_date", "endsAt", "ends_at", "finish")
_TITLE_KEYS = ("title", "summary", "name", "subject")
_CAL_KEYS = ("calendar", "calendarTitle", "calendar_title", "calendarName", "calendar_name")
_LOC_KEYS = ("location", "locationName", "place")
_NOTES_KEYS = ("notes", "description", "note")
_ALLDAY_KEYS = ("all_day", "allDay", "isAllDay", "is_all_day")


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("title", "name", "label"):
            if value.get(key):
                return str(value[key])
        return None
    return str(value)


def _to_event(raw: dict[str, Any]) -> Event:
    start = _as_text(_first(raw, _START_KEYS)) or ""
    end = _as_text(_first(raw, _END_KEYS)) or start
    return Event(
        start=start,
        end=end,
        title=_as_text(_first(raw, _TITLE_KEYS)) or "(untitled)",
        calendar=_as_text(_first(raw, _CAL_KEYS)) or "",
        location=_as_text(_first(raw, _LOC_KEYS)),
        notes=_as_text(_first(raw, _NOTES_KEYS)),
        all_day=bool(_first(raw, _ALLDAY_KEYS) or False),
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return parse_iso(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------
# course tagging
# --------------------------------------------------------------------------
def _number_variants(number: str) -> set[str]:
    number = str(number).strip()
    if not number:
        return set()
    plain = number.replace(".", "").replace("_", "")
    return {number.lower(), number.replace(".", "_").lower(), plain.lower()}


def _course_tagger(settings: Settings):
    entries = load_course_map(settings)

    table: list[tuple[str, list[str]]] = []
    for entry in entries:
        folder = str(entry.get("folder") or "").strip()
        if not folder:
            continue
        needles = {folder.lower()}
        needles |= _number_variants(str(entry.get("course_number") or ""))
        for alias in entry.get("aliases") or []:
            text = str(alias).strip().lower()
            if text:
                needles.add(text)
                needles |= _number_variants(text)
        table.append((folder, sorted(n for n in needles if len(n) >= 3)))

    def tag(event: Event) -> str | None:
        haystack = " ".join(
            part.lower() for part in (event.title, event.location, event.notes) if part
        )
        squashed = re.sub(r"[^a-z0-9]+", "", haystack)
        best: tuple[int, str] | None = None
        for folder, needles in table:
            for needle in needles:
                hit = needle in haystack or re.sub(r"[^a-z0-9]+", "", needle) in squashed
                if hit and (best is None or len(needle) > best[0]):
                    best = (len(needle), folder)
        return best[1] if best else None

    return tag


def tag_course(settings: Settings, event: Event) -> str | None:
    """The workspace course folder an event belongs to, or ``None``."""
    return _course_tagger(settings)(event)

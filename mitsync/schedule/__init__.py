"""
# Schedule

What is due and what is happening -- the time-shaped view of the workspace.

## 1. What This Module Does

`calendar` shells out to `ical-guy` (EventKit) and normalises its JSON into
events tagged to courses. `deadlines` merges Canvas planner items and
assignments with those events into `_kb/due.json`, and returns the student's
own submission status and the provenance-tagged files in each course folder as
plain data for the agent to read.

## 2. Why This Module Exists

Apple Calendar is **read-only, and there is no write path in the code to
disable**: nothing here should ever be able to move a class. EventKit is the
right layer because it sees the already-merged set of calendars Calendar.app
syncs -- MIT Exchange, iCloud, subscriptions -- with no per-account auth at
all.

## 3. How It Fits in the Architecture

This package reads only local state: the `_meta/` JSON the Canvas mirror
already wrote, plus the calendar. It deliberately imports neither `canvas.sync`
nor `httpx` -- a test asserts that in a subprocess -- so deadlines can be
produced with no network and no Canvas token.

## 4. Key Concepts

**An unavailable calendar is a correct answer.** A TCC denial or a missing
binary degrades to a warning; it never invents an event. This is
a product contract, not a swallowed error.

**`ical-guy` wants local date-only arguments.** Full ISO timestamps are
rejected, and a UTC date silently shifts the window by a day every evening in
America/New_York. `docs/API_NOTES.md` records the verified invocation.
"""

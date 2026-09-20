---
name: mit-briefing
description: Build and read today's MIT course briefing — what is due in the next 7 days plus today's classes — using `mitsync due`, `mitsync brief`, and `mitsync calendar`. Flags a stale last-sync instead of presenting old data as current. Use when the user asks what is due, what is on today, what they should work on, or for a morning rundown.
user-invocable: true
metadata:
  { "openclaw": { "requires": { "bins": ["uv"] }, "os": ["darwin"] } }
---

# mit-briefing

Answers "what's due and what's on today?" from
`/Users/filippostrub/Desktop/MIT/courses/_kb/`.

Both commands are **pure I/O**: `due` and `brief` take no judgment driver and
never exit 20. Read the briefing file rather than re-deriving its contents.

## The sequence

```
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync due
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync brief
```

- `mitsync due` merges Canvas planner items, assignments, and calendar into
  `_kb/due.json`. It takes **no flags** (only `--help`).
- `mitsync brief` writes `_kb/briefings/<YYYY-MM-DD>.md` and combines deadlines
  with calendar. It also takes **no flags**.

Then read today's file — for 2026-09-20 that is:

```
/Users/filippostrub/Desktop/MIT/courses/_kb/briefings/2026-09-20.md
```

For a raw calendar view, `mitsync calendar` accepts `--days <int>` (lookahead
window, default `calendar.lookahead_days` = 14) and `--json` (raw JSON). There
is no `--course` flag on `brief`, `due`, or `calendar`; filter by course
yourself when reporting.

## What to report

1. **Due in the next 7 days**, grouped by course, soonest first, with the day
   of week ("Thu 24 Sep") not just a date. Anything due in under 24 hours goes
   first and gets called out.
2. **Today's classes** from the calendar section of the briefing — time, course,
   location if present.
3. **The gaps section** of the briefing, if it has one. The briefing reports its
   own gaps (a missing `_meta` file, an empty manifest); pass those through
   rather than silently presenting an incomplete list as complete.

Keep it short. A briefing the user skims is worth more than a complete one they
do not read.

## Staleness — flag it, never paper over it

`_kb/due.json` carries a `last_sync` timestamp, and `brief` prints it with an
age: `fresh` if under 24 hours, otherwise `**N days old**`.

**If the last sync is not fresh, say so in your first sentence** — e.g. "Heads
up: the last Canvas sync was 3.2 days ago, so this may be missing recent
assignments." Then offer to run `mit-canvas-sync` first. Never present stale
deadlines as current, and never quietly re-run sync without saying you did.

If `last_sync` is missing entirely, the workspace has never been synced
successfully. Say that plainly and run `mitsync doctor`.

## The calendar is read-only, and may simply be unavailable

- Calendar access is **read-only by design**. Never add an event, never move
  one, never ask for calendar write permission.
- The reader shells out to an EventKit CLI (`calendar.cli` in
  `config/settings.yml`, default `ical-guy`). It can fail two ways:
  - **The binary is not on PATH** — `mitsync doctor` reports the calendar CLI
    as WARN. Fix: install the prebuilt `ical-guy` binary to
    `~/.local/bin/ical-guy`. Do **not** suggest `brew install ical-guy`: the
    formula builds from source and needs a newer Xcode CLT than this machine
    has.
  - **macOS TCC denied access.** The grant binds to code identity *and* path,
    so a grant made in Terminal does **not** carry to the LaunchAgent-run
    OpenClaw gateway — each context needs its own one-time interactive grant.
- When the calendar is unavailable the briefing says so explicitly
  ("Calendar unavailable — class times are not in this briefing"). **Pass that
  through verbatim.** Do not guess class times from a syllabus, from last
  week's briefing, or from memory. "I could not read your calendar" is a
  correct answer; an invented 10am lecture is not.

## Exit code 20

Exit 20 = pending judgment. See `_agent/CLAUDE.md` § The dual execution model.

None of the commands this skill wraps (`due`, `brief`, `calendar`) can produce
one — they take no judgment driver.

## Guardrails

- **Never invent class times when the calendar is unavailable.** Pass the
  briefing's "Calendar unavailable" line through verbatim.

Everything else — untrusted Canvas content, no calendar/Canvas writes, no
`nandatown`/`.venv`, no secrets, stay inside
`/Users/filippostrub/Desktop/MIT/courses` — is `_agent/CLAUDE.md`
§ "Hard guardrails".

# ADR 0003: Apple Calendar access is read-only, via EventKit, never writes

## Status

Accepted

## Context

`mitsync` needs class meeting times to build accurate briefings and
deadline context. The student's calendar already merges MIT's Exchange
calendar, iCloud, and any subscribed calendars inside Calendar.app, so
reading from that merged view (rather than talking to each backend
separately) is attractive. Several ways exist to read Apple Calendar data
on macOS, with very different reliability, permission models, and
maintenance status; and a decision was needed about whether `mitsync`
should ever be allowed to write to the calendar (e.g. to add a "materials
synced" marker or a due-date event).

## Decision

`mitsync` reads Apple Calendar strictly read-only, via EventKit, using a
maintained EventKit CLI with JSON output (`ical-guy`, with `ekctl` as a
documented alternative). `calendar_read.py` contains no EventKit write
call of any kind — not to add events, not to modify them — by design, and
a guardrail test checks this module for the absence of any write API
usage. No command, driver, or configuration flag in `mitsync` can cause a
calendar write; this is a hard invariant, not a default that can be
overridden.

Because EventKit full-calendar-access grants (macOS 14's
`requestFullAccessToEventsWithCompletion:`) are scoped by TCC to the
invoking code identity and session, the runbook requires one interactive
grant made from whichever context will run long-term (a Terminal session
for manual use, or the OpenClaw LaunchAgent's context for scheduled runs)
rather than assuming a grant transfers between contexts.

## Consequences

- The student's calendar can never be corrupted, duplicated into, or
  cluttered by `mitsync`, regardless of bugs elsewhere in the tool —
  the blast radius of any bug in this module is limited to reading wrong
  data, never writing wrong data.
- Deployment requires a manual, interactive one-time step (the TCC grant)
  per execution context; an unattended LaunchAgent that has never been
  granted access in its own context will fail closed with a clear
  `TccDeniedError`, not a silent empty result.
- A LaunchDaemon is unusable for this feature since it cannot receive a
  TCC prompt at all — scheduled calendar reads must run as a LaunchAgent
  in the GUI session, which constrains how OpenClaw's cron integration is
  wired for this specific capability (see `docs/RUNBOOK.md`).
- If the student ever wants calendar writes (e.g. an assignment-added
  event), that is out of scope for `mitsync` entirely per the PRD's
  non-goals, and would need a separate, explicitly-scoped tool.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| `icalBuddy` | Unmaintained; reads Calendar's sqlite cache directly, requiring the broader Full Disk Access permission instead of the narrower Calendars permission that principle-of-least-privilege favors |
| AppleScript against Calendar.app | Documented to be multi-second to outright hanging when the calendar being queried is Exchange-backed, which MIT's calendar is |
| Reading `~/Library/Calendars` directly | Requires Full Disk Access; the on-disk format is private and undocumented, not a supported integration surface, and would break on any macOS calendar-storage change |
| CalDAV against MIT's Exchange/M365 backend | Microsoft has never supported CalDAV for Exchange/M365 accounts; the supported network path (Exchange Web Services) is being retired in October 2026 in favor of Microsoft Graph, which would require registering an app in MIT's Entra tenant — a registration MIT's tenant policy may block for a student account (unverified), and which reintroduces a separate per-backend auth flow that reading through EventKit's merged view avoids entirely |
| Allowing calendar writes (e.g. sync status events) | Rejected as a non-goal: the risk of any write bug corrupting the student's real calendar was judged to outweigh the marginal convenience, and nothing in the project's requirements needs a calendar write |

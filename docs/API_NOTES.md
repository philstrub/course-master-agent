# API Notes

Research backing `canvas_client.py`, `calendar_read.py`, and the optional
OpenClaw integration. Everything here is either drawn from cited docs or
explicitly marked UNVERIFIED — do not treat an unverified item as fact when
implementing against it; validate empirically first (see
`docs/RUNBOOK.md`).

## Canvas API

Base URL: `https://canvas.mit.edu/api/v1`. This is a standard Instructure
Canvas instance, so the general Canvas LMS REST API docs
(https://canvas.instructure.com/doc/api/) apply.

### Authentication

- A student self-generates a manual access token at
  Account → Settings → "+ New Access Token" in the Canvas web UI.
- Send it as `Authorization: Bearer <token>` on every request.
- Manual tokens do not expire unless an expiry date was explicitly set at
  creation time.
- Source: Canvas API authentication docs
  (https://canvas.instructure.com/doc/api/file.oauth.html).

### Course listing

- `GET /courses?enrollment_state=active&include[]=term&per_page=100`.
- There is no "current term" server-side filter. Filter client-side using
  the `term.start_at` / `term.end_at` fields returned by `include[]=term`.
- Source: Canvas API `courses` resource docs
  (https://canvas.instructure.com/doc/api/courses.html).

### Files

- `GET /courses/:id/files` — flat file listing for a course.
- Folder tree traversal: `GET /courses/:id/folders/root`, then
  `GET /folders/:id/folders` and `GET /folders/:id/files` recursively; or
  `GET /courses/:id/folders` for a flat folder listing.
- Relevant fields per file: `id`, `uuid`, `display_name`, `filename`,
  `content_type`, `size`, `folder_id`, `updated_at`, `url`.
- The `url` field is a short-lived, pre-signed download URL. It must be
  treated as ephemeral — re-resolve via `GET /files/:id` at the moment of
  download, never cache or reuse it across a sync run.
- Some courses hide the Files tab from students entirely; requests to
  `/courses/:id/files` (and the folder endpoints) on such a course return
  **403**. The fallback is to reach files indirectly through the Modules
  API (below).
- Source: Canvas API `files` resource docs
  (https://canvas.instructure.com/doc/api/files.html).

### Modules

- `GET /courses/:id/modules?include[]=items`.
- Module item `type` is one of: `File`, `Page`, `Assignment`, `Quiz`,
  `Discussion`, `ExternalUrl`, `ExternalTool`, `SubHeader`.
- For `type: File`, resolve the actual file via the item's `content_id`
  against `GET /files/:content_id`.
- For `type: Page`, resolve via the item's `page_url` against the Pages API.
- Module ordering mirrors the course syllabus structure and should be
  treated as the primary organizing signal for filing/notes, ahead of raw
  alphabetical or upload-date ordering.
- Source: Canvas API `modules` resource docs
  (https://canvas.instructure.com/doc/api/modules.html).

### Pages and announcements

- Pages: `GET /courses/:id/pages`, add `include[]=body` for full content.
- Announcements: `GET /courses/:id/announcements?context_codes[]=course_<id>&start_date=...&end_date=...`.
  The default date window is only −14 days / +28 days from now, so always
  pass explicit `start_date`/`end_date` to get a complete history.
- Sources: Canvas API `pages` resource docs
  (https://canvas.instructure.com/doc/api/pages.html) and
  `announcements` resource docs
  (https://canvas.instructure.com/doc/api/announcements.html).

### Deadlines

- `GET /planner/items?start_date=&end_date=` is the best single endpoint
  for cross-course "what's due," aggregating assignments/quizzes/etc.
  across all enrolled courses in one call. Add `filter=new_activity` to
  get only items changed since the caller's last view, useful for change
  detection.
- `GET /calendar_events` is an alternative but caps at 10
  `context_codes[]` values per request, making it awkward across many
  courses — prefer `planner/items` for the cross-course case.
- Sources: Canvas API `planner` resource docs
  (https://canvas.instructure.com/doc/api/planner.html) and
  `calendar_events` resource docs
  (https://canvas.instructure.com/doc/api/calendar_events.html).

### Pagination

- Follow the `Link` response header's `rel="next"` entry until it is
  absent. `per_page=100` is a convention to reduce round trips; the
  server-side default when `per_page` is omitted is 10.
- Source: Canvas API pagination docs
  (https://canvas.instructure.com/doc/api/file.pagination.html).

### Rate limiting

- Response headers `X-Request-Cost` and `X-Rate-Limit-Remaining` report
  throttle budget.
- Documentation is inconsistent about which HTTP status indicates
  throttling — treat **both 403 and 429** as possible throttle signals and
  apply exponential backoff for either, distinguishing a genuine
  permissions 403 (e.g. hidden Files tab) by whether the same request
  succeeds after a backoff-and-retry.
- Prefer serial (non-concurrent) requests to stay under budget.
- Source: Canvas API rate limiting docs
  (https://canvas.instructure.com/doc/api/file.throttling.html).

### Change detection

- No documented ETag or `If-Modified-Since` support. Incremental sync
  relies on comparing each file's `updated_at` and `uuid` against the
  manifest's stored values.

### Client library

- `canvasapi` (ucfopen) is the mature, actively maintained Python client. **We do not use it.**
  `mitsync` calls the REST API directly with `httpx` because the sync layer needs the raw
  `Link`, `X-Rate-Limit-Remaining` and `X-Request-Cost` headers, per-download re-resolution of
  pre-signed URLs, and streamed downloads with sha256 — all of which `canvasapi` abstracts away.
  It remains a reasonable reference implementation for endpoint shapes
  for this API and is the intended wrapper underneath `canvas_client.py`
  for the standard resources, with raw HTTP used for planner/announcement
  calls if the library lags the API.
  (https://github.com/ucfopen/canvasapi)

### UNVERIFIED — must remain marked as such in any implementation notes

- Whether `canvas.mit.edu` (MIT's specific Canvas instance) permits
  students to generate manual access tokens at all. No MIT IS&T
  documentation confirming or denying this was found. **Verify this first,
  empirically, before building anything else** — see `docs/RUNBOOK.md`.
- The actual TTL of the pre-signed file `url`.
- Which exact HTTP status MIT's instance uses for rate-limit throttling
  (403 vs 429) in practice.
- The real ceiling on `per_page` (some Canvas instances cap it below the
  requested value).
- Whether MIT's instance supports ETag / `If-Modified-Since` despite it
  being undocumented generally.

## Apple Calendar / EventKit (macOS 14)

### TCC (privacy) behavior

- A script launched from Terminal gets its calendar-access TCC prompt/grant
  attributed to the Terminal app itself, not the script — so the grant is
  tied to "Terminal.app has calendar access," not to `mitsync`.
- A LaunchDaemon runs outside any GUI session and can never be shown a TCC
  prompt — it is silently denied, permanently, with no recourse short of
  moving it to a LaunchAgent.
- A **LaunchAgent running in the user's GUI session inherits whatever TCC
  grant was made interactively in that session** — this is the only
  workable pattern for unattended (e.g. OpenClaw-scheduled) calendar reads:
  grant access once interactively, then let the LaunchAgent run long-term
  under the same code identity.

### Access levels

- macOS 14 splits EventKit calendar access into full access
  (`requestFullAccessToEventsWithCompletion:`) and write-only access.
  Reading class times requires full access.
- A bundled binary needs its own Info.plist declaring
  `NSCalendarsFullAccessUsageDescription` — the usage-description string
  is what the OS shows the user in the permission prompt.

### Why EventKit, not something else

- EventKit sees the merged view Calendar.app already presents (MIT
  Exchange calendar, iCloud, any subscribed calendars) through one
  authorization, with no separate per-account auth needed inside `mitsync`.
- Use a maintained EventKit CLI with JSON output rather than writing a
  native Swift wrapper from scratch: `ical-guy` (Swift 6, requires
  macOS 14+, supports `--format json`) or `ekctl` as an alternative.

### Rejected alternatives (and why)

| Alternative | Why rejected |
|---|---|
| `icalBuddy` | Unmaintained; reads Calendar's sqlite cache directly, which requires Full Disk Access rather than the narrower Calendars permission |
| AppleScript / driving Calendar.app directly | Documented to be multi-second to outright hanging when the query touches an Exchange-backed calendar |
| Reading `~/Library/Calendars` directly | Requires Full Disk Access; the on-disk format is private/undocumented and not a supported integration point |
| CalDAV against MIT's Exchange/M365 backend | Microsoft has never supported CalDAV for Exchange/M365; the supported network path is Exchange Web Services (EWS), which Microsoft is retiring in October 2026, with Microsoft Graph as the replacement — that would require registering an app in MIT's Entra tenant, which MIT's tenant policy may block for a student account (**UNVERIFIED**) |

## OpenClaw (optional host)

- Requires Node.js 24.16+ or 26.1+. The development machine currently has
  Node v20.19.2 — this is a blocker for OpenClaw specifically, not for
  `mitsync`, which must work without it.
- Install: `curl -fsSL https://openclaw.ai/install.sh | bash`.
- Config file: `~/.openclaw/openclaw.json`, in JSON5 (comments/trailing
  commas allowed).
- Skills are discovered from `<workspace>/skills`, each a `SKILL.md` file
  with YAML frontmatter.
- Built-in cron scheduling: e.g.
  `openclaw cron add --name mitsync-sync --cron "0 7 * * *" --tz America/New_York`.
- `tools.fs.workspaceOnly` in the config confines OpenClaw's filesystem
  access to its configured workspace directory.
- `tools.exec.mode: allowlist` binds command-execution approvals to exact
  argv + cwd combinations, not just a command name.
- `openclaw gateway install` sets up a macOS LaunchAgent for the gateway
  process; the gateway listens on port 18789 by default.

### Gotchas

- A sleeping Mac skips scheduled cron runs entirely (no catch-up-on-wake by
  default) — any cron-triggered `mitsync` job must be safe to run late or
  to be skipped, i.e. it should derive its work from current manifest
  state ("what's not yet synced") rather than from "what changed since the
  last scheduled time."
- TCC grants are bound to code identity + path, so calendar/file access
  granted to `mitsync` run from a Terminal session does not automatically
  extend to the same binary run by the LaunchAgent-managed gateway process
  — see the TCC section above; the grant must be made in the actual
  long-running context.
- `~/Desktop` is itself a TCC-gated location on macOS, independent of the
  calendar permission — first access from a new code identity can itself
  trigger a prompt or a silent denial depending on context.
- A malformed hand-edit to `openclaw.json` can crash the gateway process
  and, in doing so, silently drop previously granted Full Disk Access;
  recovery is `openclaw doctor --non-interactive`.

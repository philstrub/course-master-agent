# Runbook — mitsync

## First-time setup

1. **Validate Canvas token support before anything else** (this is
   UNVERIFIED per `docs/API_NOTES.md` — confirm empirically):
   - In the Canvas web UI, go to Account → Settings → "+ New Access
     Token" and generate a token.
   - Validate it with a minimal curl call:
     ```
     curl -s -H "Authorization: Bearer $CANVAS_TOKEN" \
       "https://canvas.mit.edu/api/v1/courses?enrollment_state=active&per_page=100" \
       | head -c 500
     ```
   - Expect a JSON array of course objects. If this fails (401/403, or the
     token screen isn't offered at all), stop — Canvas sync cannot proceed
     until this is resolved with MIT IS&T or the course instructor.

2. **Node.js upgrade (only if you plan to use OpenClaw)**:
   - Check current version: `node --version` (known baseline: v20.19.2,
     below OpenClaw's minimum).
   - Upgrade to Node 24.16+ or 26.1+ via your normal Node version manager
     (nvm/fnm/asdf) or the official installer. `mitsync` itself does not
     depend on Node; skip this entirely if not using OpenClaw.

3. **Apple Calendar access** (`ical-guy`):
   - `brew install ical-guy` (or install `ekctl` as the alternative).
   - Run one interactive command from the exact context you intend to run
     long-term (a Terminal session for manual/API-mode use; the OpenClaw
     LaunchAgent's context if you intend cron-driven syncs) so the TCC
     full-calendar-access prompt appears and is granted there:
     ```
     ical-guy list --format json --days 1
     ```
   - Approve the system prompt requesting calendar access. If it does not
     appear and the command returns empty/denied, check System Settings →
     Privacy & Security → Calendars for the exact binary and re-trigger by
     removing and re-granting.

4. **Python environment**:
   - `uv sync` from `_agent/` to install `mitsync` and its dependencies
     (owned by the implementing agent's `pyproject.toml`; run as documented
     there).

5. **Configuration**:
   - Fill in `config/courses.yml` with the Canvas course id → folder name
     mapping for the 7 courses.
   - Review `config/naming.md` and adjust the prose filing rules to match
     the student's actual preferences.
   - If using `--driver api`, set the provider and key: e.g.
     `export ANTHROPIC_API_KEY=...` and set `llm.provider: anthropic` in
     `config/settings.yml` (or the equivalent for OpenAI/Google/a custom
     OpenAI-compatible base URL).

## Daily operation

### Agent-driven mode (default, no API key)

```
mitsync sync
mitsync organize plan --driver agent
# if plan requires judgment: resolve the printed task file, then re-run
mitsync organize apply           # only after reviewing the plan output
mitsync kb build --driver agent
mitsync kb build --driver agent   # judged course notes, one course per resolve
mitsync brief
```

Whenever a command exits 20, read the printed task path, do the reasoning
it asks for, write a result JSON, and run:
```
mitsync resolve <task_file> --result <result_file>
```
then re-run the original command.

### API-driven mode

Same commands with `--driver api`; no manual `resolve` step, since the
configured provider answers judgment calls directly.

```
mitsync sync
mitsync organize plan --driver api
mitsync organize apply
mitsync kb build --driver api
mitsync kb build --driver api
mitsync brief
```

### Checking on things

```
mitsync due                 # what's due, all courses
mitsync doctor              # manifest/graph/Canvas/calendar health check
mitsync graph query --canned concepts_by_course   # or --sql "SELECT ..."
```

## OpenClaw wiring (optional)

1. Confirm Node 24.16+/26.1+ is active (`node --version`).
2. `openclaw gateway install` — sets up the LaunchAgent-managed gateway on
   port 18789.
3. Add `mitsync`'s workspace directory to `tools.fs.workspaceOnly` in
   `~/.openclaw/openclaw.json`, and add the exact `mitsync` invocations you
   want auto-approved to `tools.exec.mode: allowlist` (argv + cwd).
4. Register a SKILL.md for `mitsync` under `<workspace>/skills` if you
   want OpenClaw's chat surface to invoke it conversationally (owned by
   the implementing agent's `skills/` directory).
5. Schedule the daily sync:
   ```
   openclaw cron add --name mitsync-sync --cron "0 7 * * *" --tz America/New_York
   ```
   Point this cron entry at `mitsync sync` (no driver needed; add `--driver api` to judgment steps if a
   key is configured) followed by `mitsync kb build`. Because a sleeping
   Mac skips missed cron runs, this job must derive its work from current
   manifest state ("what's not yet synced") — which `sync`'s incremental
   design already guarantees — rather than assuming it ran at the last
   scheduled time.
6. Re-confirm calendar access under this specific LaunchAgent context (see
   setup step 3) — a grant made in a Terminal session does not carry over.

## Failure modes

| Symptom | Likely cause | Recovery command |
|---|---|---|
| `sync` fails with 401 on every request | Canvas token expired or revoked | Generate a new token in Canvas web UI, update env/`config/settings.yml`, re-run `mitsync doctor` to confirm before retrying `sync` |
| `sync` returns 403 for one course's files | That course hides the Files tab (expected, not a bug) | No action needed — `sync` should already be falling back to the Modules API for that course; confirm with `mitsync sync --course <name> --dry-run` that the Modules fallback was used |
| `sync` repeatedly hits 403/429 across many courses | Rate limiting (status is ambiguous per API_NOTES) | Let the built-in exponential backoff run; if it still fails after the retry cap, wait several minutes and re-run `mitsync sync --dry-run` to confirm the API is reachable again before a real sync |
| `calendar` returns empty or a denial error | TCC access denied or granted to the wrong code identity/context | Re-run the interactive `ical-guy list --format json --days 1` from the exact context that failed (Terminal vs LaunchAgent) to re-trigger the grant; check System Settings → Privacy & Security → Calendars |
| OpenClaw gateway won't start / silently lost Full Disk Access | A bad hand-edit to `openclaw.json`, or a crash during a config change | `openclaw doctor --non-interactive` |
| Sync appears to have "missed" a day | Mac was asleep during the scheduled cron time | No recovery needed — next `sync` run picks up everything unsynced since it works off incremental manifest state, not "since last run" |
| `manifest.duckdb` looks corrupt (`doctor` reports errors opening it) | Crash mid-write, disk issue | Delete `state/manifest.duckdb`, run `mitsync sync --full` to rebuild it from a fresh full listing (files already in `_canvas/` are re-hashed, not re-downloaded) |
| `graph.duckdb` looks corrupt or out of sync with `_kb/graph/*.jsonl` | Crash mid-projection, or an ontology migration | `mitsync graph rebuild` — the DuckDB file is always a disposable projection of the JSONL, never edited directly |

---

# OpenClaw integration

> This section supersedes the short "OpenClaw wiring (optional)" notes above
> for anything it contradicts; that section predates the scripts described
> here.

## What OpenClaw adds, and what it does not

OpenClaw is a self-hosted AI agent gateway. Wired to this repo it adds exactly
two things:

1. **Scheduling** — three cron jobs run `mitsync` unattended (sync, daily
   briefing, weekly verify), executed by the gateway rather than by a model
   sitting in a terminal.
2. **A chat surface** — four skills let you say "what's due this week?" or
   "sync Canvas and file the new slides" in plain language, in the terminal or
   in a browser.

**It is strictly optional and adds no capability.** Everything above in
"Daily operation" works with no OpenClaw installed, no Node, and no gateway.
If OpenClaw is broken, uninstalled, or you simply do not want it, run the CLI
directly — nothing in `mitsync` imports, shells out to, or checks for it.

### The pieces

| Path | What it is |
|---|---|
| `_agent/skills/` | The four `SKILL.md` skills. Canonical location; see `_agent/skills/README.md`. |
| `<workspace>/skills` | A **symlink** to `_agent/skills`, created by `install.sh`. OpenClaw discovers skills here. |
| `_agent/openclaw/openclaw.config.json5` | A merge-ready config fragment. **You merge it by hand.** |
| `_agent/openclaw/install.sh` | Idempotent setup checker. Changes almost nothing; reports everything. |
| `_agent/openclaw/cron.sh` | Registers the three jobs with `openclaw cron add`. |
| `_agent/scripts/mitsync-cron.sh` | The wrapper the scheduler actually executes. |
| `_agent/state/logs/cron-<job>.log` | Timestamped append-only log per job. |

## Setup order

Do these in order. Each step assumes the previous one passed.

1. **Node.** OpenClaw needs **Node 24.16+ or 26.1+**. The known baseline on
   this machine is **v20.19.2 via nvm, which is too old**. Upgrade with your
   own version manager (`nvm install 24 && nvm alias default 24`), open a new
   shell, and confirm with `node --version`. `install.sh` refuses to continue
   on an unsupported Node rather than half-configuring the system.
2. **Install OpenClaw** and onboard:
   ```
   curl -fsSL https://openclaw.ai/install.sh | bash
   openclaw onboard
   openclaw gateway install      # creates a macOS LaunchAgent; gateway on :18789
   ```
3. **Run the setup checker** — safe to re-run any number of times:
   ```
   cd /Users/filippostrub/Desktop/MIT/courses/_agent
   ./openclaw/install.sh
   ```
   It verifies macOS and Node, checks `openclaw` and `uv`, runs `uv sync`,
   creates the `<workspace>/skills` symlink (refusing to clobber a real
   directory), chmods the scripts, and finishes with `mitsync doctor`.
4. **Merge the config fragment by hand.** `install.sh` deliberately does not
   touch `~/.openclaw/openclaw.json`:
   ```
   cp ~/.openclaw/openclaw.json ~/.openclaw/openclaw.json.bak
   $EDITOR ~/.openclaw/openclaw.json     # merge keys from openclaw/openclaw.config.json5
   openclaw doctor
   ```
   The fragment sets `workspace`, `tools.fs.workspaceOnly: true`,
   `tools.exec.mode: "allowlist"` with exact-argv+cwd entries, the model
   default, and `env.file`.
5. **Secrets.** `CANVAS_TOKEN` goes in `~/.openclaw/.env` (mode 600), never
   committed and never in `openclaw.json`. The CLI reads it from there too: it
   loads `_agent/.env` first, then `~/.openclaw/.env`, first file to define a
   key wins, and a variable already set in the real environment (as the gateway
   and the cron wrapper do) always wins over both. So a single
   `~/.openclaw/.env` serves the gateway and a hand-run `mitsync`; keep a
   gitignored `_agent/.env` instead if you want the CLI to use a different
   token. `mitsync doctor` names the source of each secret (never its value),
   and `MITSYNC_DOTENV=0` disables `.env` loading if you need to debug
   precedence. Deliberately do **not** set `ANTHROPIC_API_KEY` in either file: with no key, mitsync's driver resolves to
   `agent`, which is the whole point of hosting it here — the OpenClaw model
   is the judge.
6. **Calendar grant, twice.** Once interactively from Terminal, once from the
   LaunchAgent gateway. See the TCC row in the failure table.
7. **Register the schedule:**
   ```
   ./openclaw/cron.sh
   ```

## The cron schedule

Registered by `openclaw/cron.sh` with `openclaw cron add ... --tz
America/New_York --session isolated`. State lives in
`~/.openclaw/cron/jobs.json`. Every job runs
`scripts/mitsync-cron.sh <subcommand>`.

| Job name | Cron | When | Runs | Why |
|---|---|---|---|---|
| `mitsync-sync` | `0 */4 * * 1-5` | every 4h, Mon–Fri | `sync`, `extract`, `organize plan`, `due` | Catch new material during the week. Plans filing but **never applies it**. |
| `mitsync-brief` | `30 7 * * *` | daily 07:30 | `due`, `brief` | Writes `_kb/briefings/<today>.md` before you get up. |
| `mitsync-verify` | `0 22 * * 0` | Sunday 22:00 | `sync --full`, `extract --force`, `graph extract`, `graph rebuild`, `kb build`, `doctor` | Weekly full re-check; the only job that rebuilds the graph and KB. |

`mitsync organize apply` appears in **no** job. Filing requires human approval,
and an unattended run cannot obtain it.

Inspect with `openclaw cron list`, `openclaw cron get <name>`, and
`openclaw cron runs <name>`. `cron.sh` is idempotent: it reads the list first
and skips names that already exist.

## Talking to it

```
openclaw agent            # chat in the terminal
openclaw dashboard        # browser UI, gateway on http://127.0.0.1:18789
```

Four skills are exposed (each also as a slash command, since `user-invocable`
defaults to true):

| Skill | Ask it |
|---|---|
| `mit-canvas-sync` | "anything new on Canvas?" — gated on `CANVAS_TOKEN` |
| `mit-organize` | "file the new slides" — plan → approval → apply |
| `mit-briefing` | "what's due this week?" |
| `mit-kb` | "what covers convex duality?" |

Only `map`, `organize plan`, and `graph extract` take a `--driver` and can exit
20. `brief` and `kb build` take no driver — `kb build` runs with no judge by
design and degrades to a skeleton note rather than failing.

## The exit-20 protocol, unattended

With no API key configured, mitsync's judgment driver resolves to `agent`. A
judgment command then writes a task file to `_agent/state/tasks/`, prints
instructions, and exits **20**.

`scripts/mitsync-cron.sh` treats 20 as a **distinct third outcome**, not a
failure:

| Wrapper exit | Meaning | What you see |
|---|---|---|
| `0` | everything completed | "mitsync <job> done" notification |
| `20` | completed as far as it could; a task awaits judgment | "judgment needed" notification |
| `1` | a real failure | "failed" notification naming the first failing step |
| `75` | another run held the lock; nothing was done | log line only |

The job's `--message` tells the isolated agent session how to handle a 20: read
the newest task file under `_agent/state/tasks/`, follow its `instructions`,
write only the JSON answer to the path in `result_path`, and run

```
uv run --project /Users/filippostrub/Desktop/MIT/courses/_agent mitsync resolve \
  <task.json> --result <task.result.json>
```

`resolve` validates against `result_schema` and replays `origin_command`
deterministically. A resolved `organize plan` produces a **plan**, not a move —
the approval gate before `organize apply` still stands.

If nobody resolves a task, nothing breaks: the next run writes another task
file and the work simply does not advance. Old task files under
`state/tasks/` are safe to inspect and delete.

## Failure table

| Symptom | Cause | Fix |
|---|---|---|
| A scheduled run silently did not happen; no log entry for that slot | **The Mac was asleep.** macOS does not replay missed cron runs. | Nothing to recover. Every job is catch-up safe: `mitsync` derives its work from manifest state, never from "time since last run", so the next run does the missed work too. Confirm with `openclaw cron runs mitsync-sync` and the gap in `state/logs/cron-sync.log`. |
| Calendar works in Terminal but `brief` from cron reports "Calendar unavailable" | **TCC grants bind to code identity + path, and the LaunchAgent-run gateway is a different identity than your terminal.** The grant did not carry over. | Grant it again *in the gateway's context*: with `openclaw gateway install` done and you at the machine, ask the running gateway (via `openclaw agent`) to read the calendar once so the prompt can appear. Verify in System Settings → Privacy & Security → Calendars that the gateway binary is listed. `~/Desktop` is also TCC-gated and the whole workspace sits under it, so the gateway may additionally need Full Disk Access. Never guess class times around this — the briefing says "Calendar unavailable" and that must be reported, not papered over. |
| Gateway will not start after a config edit; files seem to have vanished | **A hand-edited bad key in `~/.openclaw/openclaw.json`.** A crash during a config change can also silently drop Full Disk Access, which presents as missing files rather than as a parse error. | `openclaw doctor --non-interactive` repairs a bad key. If that fails, restore `~/.openclaw/openclaw.json.bak`, restart the gateway, and re-merge one key at a time. Re-check Full Disk Access afterwards. Always `openclaw doctor` after editing; never assume a clean edit. |
| `openclaw` commands fail after a shell or nvm change; gateway dies at startup | **Node version drift.** OpenClaw needs 24.16+ or 26.1+; the machine's baseline was v20.19.2 via nvm, and nvm's default can move under you. Worse, the LaunchAgent does not source your shell profile, so it never sees nvm's shims at all. | `node --version` in a fresh shell; `nvm alias default 24`. For the gateway, ensure an absolute path to a supported node binary — install Node system-wide as well as under nvm if needed. Re-run `./openclaw/install.sh`, which refuses to proceed on an unsupported Node, then `openclaw doctor`. |
| A cron job keeps ending with "judgment needed" and nothing progresses | **A task is stuck pending judgment** — the agent session never resolved it (model error, hit a limit, or the message was not acted on). | Not a failure; the work is paused, not lost. List pending tasks: `ls -t _agent/state/tasks/*.json`. A task with no sibling `.result.json` is unresolved. Resolve it yourself or from `openclaw agent`, then re-run. Exit 20 from the wrapper is expected here; check `state/logs/cron-<job>.log` for the "PENDING JUDGMENT" line naming the step. |
| Two runs appear interleaved in the log, or the wrapper exits 75 | Overlapping runs (a long `verify` still going when `sync` fires). | 75 means the lock was held and the loser did nothing — correct behavior, no action needed. The lock is `_agent/state/mitsync-cron.lock`; a stale one from a crashed run is auto-removed when its pid is gone. |
| A skill's command prompts for approval every time | `tools.exec.mode: "allowlist"` matches **exact argv + cwd**. A new or reordered flag is a different command. | Add the precise argv to the `allow` list in `~/.openclaw/openclaw.json`. Do **not** relax the mode to `auto` or `full` — the agent routinely reads untrusted Canvas text. `mitsync organize apply`, `organize plan --include-existing`, `organize undo`, and `map --apply` are deliberately absent from the allowlist and must keep prompting. |
| `mit-canvas-sync` does not appear in the skill list | Its frontmatter gates on `env: ["CANVAS_TOKEN"]`; the variable is unset in the gateway's environment. | Add `CANVAS_TOKEN=...` to `~/.openclaw/.env` (mode 600) and confirm `env.file` points there in `openclaw.json`. Restart the gateway. |
| Skills are not discovered at all | The `<workspace>/skills` symlink is missing or points elsewhere. | `ls -l /Users/filippostrub/Desktop/MIT/courses/skills` should read `-> .../_agent/skills`. Re-run `./openclaw/install.sh`; it refuses to overwrite a real directory, so move that directory aside first if one exists. |

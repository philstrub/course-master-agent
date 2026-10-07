# OpenClaw for mitsync: what it is, and how to set it up

Facts below are from docs.openclaw.ai as of 2026-09.
OpenClaw moves fast, so when this file and `openclaw <cmd> --help` disagree,
trust `--help`.

## What OpenClaw is, in one picture

```
 you (web dashboard at :18789)   cron: brief 07:00 · sync, file, forum every 30 min
                 │                                        │
                 ▼                                        ▼
        ┌──────────────── Gateway (always-on daemon, port 18789) ────────────┐
        │  receives messages & timers → runs an agent turn → replies         │
        │                                                                    │
        │  agent loop:  system prompt = AGENTS.md + SOUL.md + USER.md        │
        │               + skill list (name + description of each SKILL.md)   │
        │  model (claude CLI) ─► picks a tool ─► exec / read / write ───┐    │
        │         ▲                                                     │    │
        │         └──────────── tool result ◄───────────────────────────┘    │
        └────────────────────────────────────────────────────────────────────┘
                 │ exec (allowlisted to one binary)
                 ▼
        _agent/bin/mitsync-agent sync | due | work | email …  ──► Gmail SMTP ──► your inbox
```

- **Gateway**: a macOS LaunchAgent (`ai.openclaw.gateway`) that stays up, so
  the agent can act at 07:00 with no terminal open. It serves the web
  dashboard at http://127.0.0.1:18789.
- **Agent loop**: on each turn the model sees the bootstrap files, the skill
  list and the conversation. It calls tools (shell `exec`, file read and
  write, `memory_search`) and keeps looping until it can answer.
- **Skills**: folders that each hold a `SKILL.md`. Only the name and
  description sit in the prompt. When one matches, the model reads the full
  file and follows it. Every skill is also a slash command, e.g. `/mit-briefing`.
- **Cron**: scheduled prompts. Each run is an isolated session started with
  your message.
- **Model**: `anthropic/claude-sonnet-5` on the `claude-cli` runtime, which
  runs your local `claude` binary (Claude Code). Turns use your Claude
  subscription instead of API tokens. Every shell call Claude Code makes is
  checked against OpenClaw's exec allowlist first.
- **Channels** (Telegram, WhatsApp, …): not used. The brief arrives by email,
  sent by the `mitsync email` tool.
- **Memory**: Markdown in the workspace (`memory/<date>.md`). Used lightly;
  it's the next homework.

## Where the files live

```
~/.openclaw/                   OpenClaw's own state (never in git)
  openclaw.json                config (JSON5); merge _agent/openclaw/openclaw.json5 into it
  .env                         optional: GOOGLE_API_KEY, only for the Gemini fallback
  agents/main/…                sessions, transcripts, model auth

~/Desktop/MIT/courses/         the agent WORKSPACE (agents.defaults.workspace)
  AGENTS.md    copy of _agent/openclaw/workspace/AGENTS.md   operating rules, loaded every turn
  SOUL.md      copy of _agent/openclaw/workspace/SOUL.md     tone
  USER.md      copy of _agent/openclaw/workspace/USER.md     who you are
  IDENTITY.md  written by onboarding (the agent's name)
  skills  ->   _agent/skills (symlink)                       mit-briefing, mit-canvas-sync, …
  Machine Learning/ …  _canvas/  _kb/  _agent/        what the agent works on

~/Desktop/MIT/courses/_agent/.env    CANVAS_TOKEN, GMAIL_APP_PASSWORD (read by mitsync)
```

No model key is needed. The `claude` binary holds your subscription login in
the macOS Keychain. mitsync's secrets (the Canvas token and the Gmail
password) stay in `_agent/.env`, which OpenClaw never reads.

## Setup

**0. Node 26.** OpenClaw needs Node 24.16+ or 26.1+.

```
nvm install 26 && nvm alias default 26 && node --version
```

**1. Install OpenClaw.**

```
npm install -g openclaw@latest && openclaw --version
```

**2. Put the agent's files into the workspace.** OpenClaw refuses symlinked
bootstrap files: the model gets `[UNREADABLE: symlink path component not
allowed]` instead of your rules. So they are **copied**, and the originals
stay in git under `_agent/openclaw/workspace/`:

```
cd ~/Desktop/MIT/courses/_agent
make openclaw-workspace     # copies AGENTS.md, SOUL.md, USER.md to the workspace root
make openclaw-check         # after you edit one: says whether the copies are stale
ls -l ../skills             # skills -> …/_agent/skills (a symlink is fine for skills)
```

Edit the originals, never the copies, and re-run `make openclaw-workspace`.

**3. Onboard with your Claude subscription.** Claude Code must be logged in
on this Mac, with your Pro/Max plan and not an API key:

```
claude auth status          # "loggedIn": true, "authMethod": "claude.ai"
claude auth login           # only if it isn't
openclaw onboard --install-daemon --workspace ~/Desktop/MIT/courses
#   at the model/auth step choose: Anthropic → Claude CLI
```

If onboarding already set up another model (it may have picked Gemini),
step 4's config switches it over; no new login is needed. In an interactive
terminal you can also run
`openclaw models auth login --agent mitsync --provider anthropic --method cli --set-default`.

The gateway runs as a LaunchAgent, which has no shell profile. Your
`claude` binary is at `~/.local/bin/claude`, and that directory may not be on
the daemon's PATH. Link it next to the gateway's own `node`, which is on that
PATH:

```
ln -sf ~/.local/bin/claude ~/.nvm/versions/node/v26.10.0/bin/claude
```

Before relying on the subscription:
- **Usage limits.** Each turn uses your plan's limits, the same pool as your
  interactive Claude Code sessions. A morning brief is roughly 10 turns of
  Sonnet: a small share of a day's allowance. If a brief ever fails on a
  limit, the run is simply retried the next morning, or by hand.
- **Terms.** Using a subscription from a third-party tool is Anthropic's call,
  and it can change. OpenClaw's docs say so too. Check Anthropic's current
  usage policy before relying on this for anything beyond coursework.
- **Optional fallback.** `openclaw.json5` has a commented-out fallback to
  Gemini Flash's free tier. To enable it, copy the key line without printing
  it (`grep '^GOOGLE_API_KEY=' _agent/.env >> ~/.openclaw/.env`) and uncomment
  `fallbacks`. Free-tier prompts may be used to train Google's models.

**4. Config.** Apply the keys from `_agent/openclaw/openclaw.json5` (back up
first). `--replace` is needed because onboarding's model list gets replaced:

```
cp ~/.openclaw/openclaw.json ~/.openclaw/openclaw.json.bak
openclaw config set agents.defaults.model '{"primary": "anthropic/claude-sonnet-5"}'
openclaw config set agents.defaults.models \
  '{"anthropic/claude-sonnet-5": {"agentRuntime": {"id": "claude-cli"}}}' --replace
openclaw config set agents.defaults.heartbeat.every 0m
openclaw config set tools.exec.mode ask
openclaw config validate
```

Exec mode is `ask`, not `allowlist`, on purpose. With the Claude CLI runtime,
`allowlist` (ask off) denies *every* shell call, the wrapper included. `ask`
runs allowlisted commands and holds anything else for your approval in the
dashboard. When nobody can answer, as in a cron run, the default
`askFallback` is `deny`.

Then allowlist the one command, for the agent onboarding created (`openclaw
agents list` shows it; here it is `mitsync`), and check:

```
openclaw approvals allowlist add --agent mitsync ~/Desktop/MIT/courses/_agent/bin/mitsync-agent
openclaw models list | head -3               # anthropic/claude-sonnet-5 … default,configured
openclaw models status --agent mitsync       # "anthropic via claude-cli"; "indeterminate" is normal
openclaw skills list | grep mit-             # mit-briefing "ready"
```

**Smoke test** (three short turns on your subscription):

```
openclaw agent --agent mitsync --session-id "$(uuidgen)" --message \
  "Run (a) ls ~ and (b) ~/Desktop/MIT/courses/_agent/bin/mitsync-agent doctor; say which ran."
```

Expected: (a) denied ("approval was not granted"), (b) runs. If both are
denied, exec mode is still `allowlist`. If both run, the allowlist is not
being enforced: stop and fix that before step 9.

**5. Gmail app password (the delivery).** The brief is sent from your Gmail
to your Gmail (`email.sender` and `email.to` in `config/settings.yml`). Gmail
won't accept your real password for this; it needs an *app password*:

1. Google Account → Security → turn on **2-Step Verification** (required).
2. Go to myaccount.google.com/apppasswords → create one called "mitsync".
3. Add it to `_agent/.env`: `GMAIL_APP_PASSWORD=abcdabcdabcdabcd` (no spaces).

The renderer is already set up: `email/node_modules` is installed, and
`email.node` points at Node 26 by absolute path.

**6. macOS permissions (the usual snag).** The gateway is a background
process, not your Terminal, so it holds its own privacy grants.

*Full Disk Access* (for `~/Desktop`): System Settings → Privacy & Security →
**Full Disk Access** → **+** → press **⌘⇧G**, paste
`~/.nvm/versions/node/v26.10.0/bin/node` → **Open**, switch it on, then run
`openclaw gateway restart`. This is the binary `openclaw gateway status
--deep` names. Upgrading Node changes the path, so grant it again after.

*Calendar* can't be granted the same way. macOS credits a Calendar request to
the *responsible* process, which for the gateway is that bare `node`: it gets
refused silently, with no prompt, and the Calendars pane has no **+**. So
mitsync reads Calendar through a tiny read-only app that owns its own grant:

```
cd ~/Desktop/MIT/courses/_agent
make calendar-helper                        # builds ~/Applications/MitsyncCalendar.app
open ~/Applications/MitsyncCalendar.app     # click OK on "MitsyncCalendar would like to access your calendar"
bin/mitsync-calendar events list --from today --to today --format json --group-by none | head -c 300
```

`calendar.cli` in `config/settings.yml` already points at
`bin/mitsync-calendar`, which launches the app through `open`. That makes the
app, not `node`, the one asking. A rebuild is a new app to macOS and needs OK
again. If you skip this, the brief still works and says "calendar
unavailable".

**7. Test the tools without the agent.**

```
cd ~/Desktop/MIT/courses/_agent
uv run mitsync doctor                    # every row ok (calendar may warn)
bin/mitsync-agent due --days 7 --json | head -40
bin/mitsync-agent organize undo             # must be REFUSED by the wrapper
```

**8. Schedule it.** Eight jobs. Four wake a model, and three of those only
when there is something new. Every sync also refreshes the knowledge graph
(`extract`, backbone, DuckDB, Neo4j), so the graph is never older than the
last sync, and `graph-build` adds the judgment the folders cannot give:

| job | when (New York) | what | model |
|---|---|---|---|
| `gradescope-sync`, `-morning` | 5 min before each sync | `mitsync-agent gradescope sync` (command job), so the backbone's Assignment nodes carry Gradescope's status | none |
| `canvas-sync-morning` | 06:45 Mon–Fri | `mitsync-agent sync`: mirror Canvas, then refresh the graph (command job) | none |
| `morning-brief` | 07:00 Mon–Fri | `mit-briefing`: judge, write, email | Sonnet 5, low thinking |
| `canvas-sync` | every 30 min, 08:00–22:30 | `mitsync-agent sync`: mirror Canvas, then refresh the graph (command job) | none |
| `canvas-file` | 10 min after each sync | `mit-organize`: file new Canvas material | Haiku 4.5, low thinking, **only if** `openclaw/triggers/new-to-file.js` sees a file no plan has filed, skipped or left out yet. Skips its turn while a sync holds the manifest; errors after 3 runs in a row leave the same files undecided |
| `graph-build` | 20 min after each sync | `mit-graph-build`: close what `graph check` lists, one subagent per reading-heavy course | Sonnet 5, low thinking, **only if** `openclaw/triggers/graph-pending.js` (which runs `graph refresh`, then `graph check`) sees an item the last check hadn't. Skips its turn while a sync holds a database |
| `canvas-forum` | :15 and :45, 08:15–22:45 | the separate `forum` agent: read the Homework 3 discussion, post if it has something useful, else record a skip (see "The forum agent" below) | Sonnet 5, medium thinking, **only if** `openclaw/triggers/forum-new.js` (`forum pending`) sees unseen entries by others, the control line reads RUNNING and the agent has not stopped |
| `course-notes` | 23:00 daily | `mit-course`: bring each course's master file up to date | Sonnet 5, low thinking, **only if** `openclaw/triggers/course-pending.js` (which runs `extract`, `kb build`, `kb check`) sees a document the last check hadn't |

Every agent job carries a tool allow-list (`--tools exec,read,write`). With
the Claude CLI runtime that switches off Claude Code's own tools (Skill, Read,
Write, Bash), each of which asks a human, waits 120 s and is denied in an
unattended run. Leaving `process` out also makes `exec` wait for each command
instead of backgrounding it after 10 s.

```
W=~/Desktop/MIT/courses/_agent/bin/mitsync-agent
openclaw config set agents.defaults.models \
  '{"anthropic/claude-haiku-4-5":{"agentRuntime":{"id":"claude-cli"},"alias":"haiku"}}' --strict-json --merge

openclaw cron add --name gradescope-sync-morning --agent mitsync --cron "40 6 * * 1-5" \
  --tz America/New_York --exact --command-argv "[\"$W\",\"gradescope\",\"sync\"]" --timeout-seconds 120 --no-deliver
openclaw cron add --name gradescope-sync --agent mitsync --cron "25,55 7-22 * * *" \
  --tz America/New_York --exact --command-argv "[\"$W\",\"gradescope\",\"sync\"]" --timeout-seconds 120 --no-deliver

openclaw cron add --name canvas-sync-morning --agent mitsync --cron "45 6 * * 1-5" \
  --tz America/New_York --exact --command-argv "[\"$W\",\"sync\"]" --timeout-seconds 600 --no-deliver
openclaw cron add --name canvas-sync --agent mitsync --cron "*/30 8-22 * * *" \
  --tz America/New_York --exact --command-argv "[\"$W\",\"sync\"]" --timeout-seconds 600 --no-deliver

openclaw cron add --name morning-brief --agent mitsync --cron "0 7 * * 1-5" \
  --tz America/New_York --exact --session isolated \
  --message "Scheduled run, nobody is watching. Read skills/mit-briefing/SKILL.md and follow it: write today's morning brief and send it. Stay within its tool budget." \
  --thinking low --tools exec,read,write --timeout-seconds 900 --no-deliver

openclaw cron add --name canvas-file --agent mitsync --cron "10,40 8-22 * * *" \
  --tz America/New_York --exact --session isolated \
  --trigger-script ./openclaw/triggers/new-to-file.js \
  --message "Scheduled filing run, nobody is watching. Read skills/mit-organize/SKILL.md and follow it: file the new Canvas material with organize apply --yes." \
  --model anthropic/claude-haiku-4-5 --thinking low --tools exec,read,write --timeout-seconds 600 --no-deliver

openclaw cron add --name graph-build --agent mitsync --cron "20,50 8-22 * * *" \
  --tz America/New_York --exact --session isolated \
  --trigger-script ./openclaw/triggers/graph-pending.js \
  --message "Scheduled run, nobody is watching. Read skills/mit-graph-build/SKILL.md and follow its scheduled run: close what graph check lists, delegating reading-heavy courses to subagents, and check every facts file before you add it." \
  --thinking low --tools exec,read,write,sessions_spawn,sessions_yield --timeout-seconds 1800 --no-deliver

openclaw cron add --name course-notes --agent mitsync --cron "0 23 * * *" \
  --tz America/New_York --exact --session isolated \
  --trigger-script ./openclaw/triggers/course-pending.js \
  --message "Scheduled run, nobody is watching. Read skills/mit-course/SKILL.md and follow it: bring the course master files up to date. Stay within its budget of 8 documents." \
  --thinking low --tools exec,read,write --timeout-seconds 1800 --no-deliver

openclaw cron list                                   # note the ids
openclaw cron run <id> --wait --wait-timeout 15m     # run one now
openclaw cron runs <id>                              # history and tool trace
```

`--no-deliver` everywhere: the brief emails itself, and the others have
nothing to say. `canvas-file` runs `organize apply --yes`, which only links
Canvas files out of the mirror and rejects anything that touches a file of
yours. Each apply writes an undo log. **Undo is yours**:
`uv run mitsync organize undo` from `_agent/`.

The brief is cheap because the skill doesn't sync (06:45 did), reads 7 days
not 14, reuses yesterday's judgment for any homework with no new files, and
lets `email` validate instead of a separate dry run. That is about 8 tool
calls on an ordinary day.

**Watch a run live.** `openclaw sessions tail` records nothing for the Claude
CLI runtime, but each turn is a real `claude` run whose transcript lands in
`~/.claude/projects/-Users-filippostrub-Desktop-MIT-courses/`.
`bin/openclaw-watch` follows those files and prints every tool call and
result as it happens (`--last` replays the newest run). For the report's
screenshot, run it in one terminal and trigger the brief from another:

```
~/Desktop/MIT/courses/_agent/bin/openclaw-watch        # terminal 1: live trace
rm ~/Desktop/MIT/courses/_agent/state/sent/$(date +%F).json   # only if today's brief already went out
openclaw cron run c314b76f-2fc0-4c60-ad39-0ff4a7db9bfe       # terminal 2: start the brief
```

**9. Chat with it (optional).** `openclaw dashboard` opens the web chat at
http://127.0.0.1:18789. A chat turn has no `--tools` list, so Claude Code's
own tools each ask you: click **Allow** on each Skill, Read or Write card.
Shell commands still go only through the allowlist.

The Mac must be awake at 06:45, because a sleeping laptop skips the run. To
wake it a minute early:

```
sudo pmset repeat wakeorpoweron MTWRF 06:44:00
```

## The forum agent (Homework 3)

Homework 3 asks for an agent that takes part in the Canvas discussion
**Homework 3: Agent Discussion Forum**
(https://canvas.mit.edu/courses/40577/discussion_topics/448963) on a
schedule, remembers what it has seen and done, and stops safely. That is
mitsync's only Canvas write, so it runs as a **second OpenClaw agent** in the
same gateway, with the least it needs:

| | `mitsync` (everything else) | `forum` (Homework 3) |
|---|---|---|
| workspace | `~/Desktop/MIT/courses` | `~/Desktop/MIT/courses/_kb/forum` |
| bootstrap | `AGENTS.md`, `SOUL.md`, `USER.md` | `openclaw/forum/AGENTS.md` (procedure, posting rule, boundaries), `SOUL.md`, an empty `USER.md`: it is told nothing personal |
| exec allowlist | `bin/mitsync-agent` (refuses `forum`) | `bin/mitsync-forum`: only `forum pending · read · act · scholar · knowledge` |
| file tools | the workspace | `tools.fs.workspaceOnly`: `_kb/forum/` only, so grades, briefs, calendar and homework are unreachable |
| reaches courses through | everything | `forum knowledge`: lecture concepts and passages, no assignments, no paths |
| Canvas | read-only | read, plus entries in topic 448963 (`forum act`) |

`forum act` enforces in code what the assignment and the student require,
whatever the decision file says. It re-reads the control line before every
attempt and refuses unless it reads `COURSE-TEAM CONTROL: RUNNING`. It allows
at most 3 posts in any hour (counted from Canvas), retries with backoff, and
stops after 3 consecutive failures until you run `uv run mitsync forum reset`.
It refuses replies to the agent's own posts, second replies, and repeats. Its
content screen rejects emails, phones, secrets, paths, file names, grades, "my
homework/calendar" and non-paper links. Every post is read back from Canvas
before it counts.

**Memory**, separate from the knowledge graph:

| file | written by | holds |
|---|---|---|
| `_kb/forum/diary.md` | `forum act`, from the agent's decision | one entry per run: what the forum discussed, the threads followed, what it posted (quoted) or why it skipped, its sources. `forum read` hands the tail back to the agent |
| `_kb/forum/posts.jsonl` | `forum act` | every post: entry id, parent, link, attempts, verified, recovered, text |
| `_kb/forum/decisions/*.json` | the agent | each run's decision, as validated |
| `_agent/state/forum/state.json` | `forum` commands | seen entry ids, failure count, the in-flight post (write-ahead intent). Outside the agent's reach |

**Set it up** (from `_agent/`):

```
uv run mitsync forum pending          # creates _kb/forum/, checks the token and the control line
openclaw agents add forum --workspace ~/Desktop/MIT/courses/_kb/forum \
  --model anthropic/claude-sonnet-5 --non-interactive
make openclaw-forum                   # AGENTS.md, SOUL.md, USER.md into _kb/forum/ (after onboarding's templates)
ln -s _kb/forum ~/Desktop/MIT/courses/forum   # script/trigger runtime resolves <workspace>/<agent id>; point it at the real one
openclaw config set agents.entries.forum.tools '{"fs":{"workspaceOnly":true}}' --strict-json --merge
openclaw config set agents.entries.forum.skills '[]' --strict-json
openclaw config set agents.entries.forum.heartbeat '{"every":"0m"}' --strict-json --merge
openclaw agents set-identity --agent forum --name mitsync-forum
openclaw approvals allowlist add --agent forum ~/Desktop/MIT/courses/_agent/bin/mitsync-forum
openclaw approvals get                # must show NO row for agent "*"; remove one with
                                      #   openclaw approvals allowlist remove --agent '*' <path>

openclaw cron add --name canvas-forum --agent forum --cron "15,45 8-22 * * *" \
  --tz America/New_York --exact --session isolated \
  --trigger-script ./openclaw/triggers/forum-new.js \
  --message "Scheduled forum run, nobody is watching. Follow your AGENTS.md: read the forum, decide whether you have something relevant and useful to add, and hand your decision to forum act. Skipping is a normal outcome." \
  --thinking medium --tools exec,read,write --timeout-seconds 900 --no-deliver
```

**Check the boundary before the first post.** Run one turn asking the agent
to (a) read `~/Desktop/MIT/courses/_agent/state/gradescope.json`, (b) exec
`bin/mitsync-agent due`, (c) exec `bin/mitsync-forum due`, (d) exec
`bin/mitsync-forum forum pending`, (e) exec `ls ~`. Only (d) may succeed.
Read the result in the transcript (`bin/openclaw-watch --last`), not in the
agent's own summary: in testing, the agent misreported which calls had been
denied. A row for agent `*` in `openclaw approvals get` let (b) run until it
was removed.

**Preview, then go live.** Write a decision file and check it with
`forum act --decision <file> --dry-run`: every bound is checked, and the
command prints the HTML it would post and sends nothing.

**Recovery demo.** `MITSYNC_FORUM_FAULT=lost-ack uv run mitsync forum act
--decision <file>` makes the first attempt time out after Canvas saved the
post. The tool re-reads the topic at 5, 15 and 30 s; if it finds its entry it
logs it, and if Canvas's cached view still hides it, it stops with "outcome
unknown" and never retries: the next run reconciles it. Either way there is one
post. `=crash` exits right after the POST. The next `forum read` or `forum
pending` finds the entry and logs it (`"recovered"`).

| symptom | fix |
|---|---|
| `forum act` refused: control line is PAUSED / MISSING | the course team paused the forum; the agent records a skip. Nothing to do |
| `stopped after 3 consecutive failed posts` | read the `FAILED` diary entries, fix the cause, then `uv run mitsync forum reset` |
| `forum scholar` says CAPTCHA | Scholar is rate-limiting this IP; the agent posts from the course knowledge alone until it clears |
| trigger fails with `WORKSPACE_VANISHED` for `courses/forum` | the script runtime uses `<default workspace>/forum`, not `_kb/forum`: restore the symlink `ln -s _kb/forum ~/Desktop/MIT/courses/forum`. Never delete that path |
| the forum agent ran a non-forum command | `openclaw approvals get` shows an allowlist row for agent `*` or `forum` other than `mitsync-forum`; remove it |

## When something breaks

| symptom | fix |
|---|---|
| `openclaw: command not found` | nvm isn't on Node 26 in this shell: `nvm use 26` |
| `claude: command not found` / CLI backend unavailable | the daemon can't see `claude`: redo the `ln -sf` in step 3, then `openclaw gateway restart` |
| "not logged in" / auth error | `claude auth login` in a terminal (the same Mac user), then restart the gateway |
| usage limit reached | your plan's limit is spent: re-run later with `openclaw cron run <id>`, or enable the Gemini fallback (step 3) |
| "unknown model" | `openclaw models list`; `model.primary` must be `anthropic/claude-sonnet-5` with `agentRuntime: claude-cli` (step 4) |
| agent says AGENTS.md is "UNREADABLE: symlink…" | the bootstrap files are symlinks: `make openclaw-workspace` (step 2), then start a new session |
| every command denied, "security=allowlist, ask=off" | `openclaw config set tools.exec.mode ask` (step 4) |
| cron run takes many minutes, log shows `plugin.approval.waitDecision 120000ms` | the job has no `--tools` list, so native tools wait on approvals: `openclaw cron edit <id> --tools exec,read,write` |
| `due`/`work` fail with "Could not set lock on file" | `sync` is still running (a `canvas-sync` job, or one `exec` backgrounded): leave `process` out of `--tools` so `exec` waits, and keep the brief off the sync slots |
| `canvas-sync` run shows `error` with `StalePresignedURL` | Canvas gave no download link for a just-published file; the next sync retries it |
| exec fails with `host=node requires a paired node` | `openclaw config set tools.exec.host gateway` |
| `canvas-file` never runs | expected when nothing new arrived: its trigger stays quiet. `openclaw cron runs <id>` shows fired runs only |
| agent still behaves as before a fix | the chat reuses its session; start a new chat in the dashboard, or pass `--session-id "$(uuidgen)"` |
| exec "denied" / "not allowlisted" | the command wasn't `…/_agent/bin/mitsync-agent`; redo the allowlist in step 4 |
| sync finds nothing / files missing | Full Disk Access for the gateway's `node` (step 6) |
| "calendar unavailable" / "denied calendar access to MitsyncCalendar.app" | `open ~/Applications/MitsyncCalendar.app` and click OK, or turn it on in Privacy & Security → Calendars (step 6) |
| "MitsyncCalendar.app not found" | `make calendar-helper` (step 6) |
| no email | `uv run mitsync email --dry-run` names the problem: no brief JSON, a schema error, no `GMAIL_APP_PASSWORD`, or a Node path. `state/sent/<date>.json` means today's was already sent (`--resend`) |
| Gmail "Username and Password not accepted" | the password is your real one or has spaces; make an app password (step 5) |
| anything else | `openclaw doctor`, `openclaw logs --follow`, `openclaw status --deep` |

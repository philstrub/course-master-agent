# OpenClaw for mitsync: what it is, and how to set it up

Facts below are from docs.openclaw.ai as of 2026-09.
OpenClaw moves fast, so when this file and `openclaw <cmd> --help` disagree,
trust `--help`.

## What OpenClaw is, in one picture

```
 you (web dashboard at :18789)         cron: brief 07:00 · sync + file every 2 h
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

**8. Schedule it.** Five jobs. Three wake a model, and two of those only
when there is something new:

| job | when (New York) | what | model |
|---|---|---|---|
| `canvas-sync-morning` | 06:45 Mon–Fri | `mitsync-agent sync` (command job) | none |
| `morning-brief` | 07:00 Mon–Fri | `mit-briefing`: judge, write, email | Sonnet 5, low thinking |
| `canvas-sync` | every 2 h, 08:00–22:00 | `mitsync-agent sync` (command job) | none |
| `canvas-file` | 10 min after each sync | `mit-organize`: file new Canvas material | Haiku 4.5, low thinking, **only if** `openclaw/triggers/new-to-file.js` sees a file the last check hadn't |
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

openclaw cron add --name canvas-sync-morning --agent mitsync --cron "45 6 * * 1-5" \
  --tz America/New_York --exact --command-argv "[\"$W\",\"sync\"]" --timeout-seconds 600 --no-deliver
openclaw cron add --name canvas-sync --agent mitsync --cron "0 8-22/2 * * *" \
  --tz America/New_York --exact --command-argv "[\"$W\",\"sync\"]" --timeout-seconds 600 --no-deliver

openclaw cron add --name morning-brief --agent mitsync --cron "0 7 * * 1-5" \
  --tz America/New_York --exact --session isolated \
  --message "Scheduled run, nobody is watching. Read skills/mit-briefing/SKILL.md and follow it: write today's morning brief and send it. Stay within its tool budget." \
  --thinking low --tools exec,read,write --timeout-seconds 900 --no-deliver

openclaw cron add --name canvas-file --agent mitsync --cron "10 8-22/2 * * *" \
  --tz America/New_York --exact --session isolated \
  --trigger-script ./openclaw/triggers/new-to-file.js \
  --message "Scheduled filing run, nobody is watching. Read skills/mit-organize/SKILL.md and follow it: file the new Canvas material with organize apply --yes." \
  --model anthropic/claude-haiku-4-5 --thinking low --tools exec,read,write --timeout-seconds 600 --no-deliver

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

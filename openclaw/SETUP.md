# OpenClaw for mitsync: what it is, and how to set it up

Facts below are from docs.openclaw.ai and ai.google.dev as of 2026-09.
OpenClaw moves fast, so when this file and `openclaw <cmd> --help` disagree,
trust `--help`.

## What OpenClaw is, in one picture

```
 you (web dashboard at :18789)                       cron: "30 7 * * 1-5"
                 │                                        │
                 ▼                                        ▼
        ┌──────────────── Gateway (always-on daemon, port 18789) ────────────┐
        │  receives messages & timers → runs an agent turn → replies         │
        │                                                                    │
        │  agent loop:  system prompt = AGENTS.md + SOUL.md + USER.md        │
        │               + skill list (name + description of each SKILL.md)   │
        │     model (Gemini) ──► picks a tool ──► exec / read / write ──┐    │
        │         ▲                                                     │    │
        │         └──────────── tool result ◄───────────────────────────┘    │
        └────────────────────────────────────────────────────────────────────┘
                 │ exec (allowlisted to one binary)
                 ▼
        _agent/bin/mitsync-agent sync | due | work | email …  ──► Gmail SMTP ──► your inbox
```

- **Gateway**: a macOS LaunchAgent (`ai.openclaw.gateway`) that stays up, so
  the agent can act at 07:30 with no terminal open. It serves the web
  dashboard at http://127.0.0.1:18789.
- **Agent loop**: on each turn the model sees the bootstrap files, the skill
  list and the conversation. It calls tools (shell `exec`, file read and
  write, `memory_search`) and keeps looping until it can answer.
- **Skills**: folders that each hold a `SKILL.md`. Only the name and
  description sit in the prompt. When one matches, the model reads the full
  file and follows it. Every skill is also a slash command, e.g. `/mit-briefing`.
- **Cron**: scheduled prompts. Each run is an isolated session started with
  your message.
- **Channels** (Telegram, WhatsApp, …): not used. The brief arrives by email,
  sent by the `mitsync email` tool.
- **Memory**: Markdown in the workspace (`memory/<date>.md`). Used lightly;
  it's the next homework.

## Where the files live

```
~/.openclaw/                   OpenClaw's own state (never in git)
  openclaw.json                config (JSON5); merge _agent/openclaw/openclaw.json5 into it
  .env                         GOOGLE_API_KEY, read by the gateway daemon
  agents/main/…                sessions, transcripts, model auth

~/Desktop/MIT/courses/         the agent WORKSPACE (agents.defaults.workspace)
  AGENTS.md  -> _agent/openclaw/workspace/AGENTS.md   operating rules, loaded every turn
  SOUL.md    -> _agent/openclaw/workspace/SOUL.md     tone
  USER.md    -> _agent/openclaw/workspace/USER.md     who you are
  skills     -> _agent/skills                         mit-briefing, mit-canvas-sync, …
  Machine Learning/ …  _canvas/  _kb/  _agent/        what the agent works on

~/Desktop/MIT/courses/_agent/.env    CANVAS_TOKEN, GMAIL_APP_PASSWORD (read by mitsync)
```

There are two `.env` files because two programs need secrets. **OpenClaw**
needs the model key. **mitsync** needs the Canvas token and the Gmail
password. Neither one reads the other's file.

## Setup

**0. Node 26.** OpenClaw needs Node 24.16+ or 26.1+.

```
nvm install 26 && nvm alias default 26 && node --version
```

**1. Install OpenClaw.**

```
npm install -g openclaw@latest && openclaw --version
```

**2. Link the agent's files into the workspace.** Do this before onboarding,
so OpenClaw finds these files instead of writing its own templates:

```
cd ~/Desktop/MIT/courses
for f in AGENTS SOUL USER; do ln -sf _agent/openclaw/workspace/$f.md $f.md; done
ls -l AGENTS.md SOUL.md USER.md skills     # all four are symlinks into _agent/
```

**3. Onboard with the Gemini key.** The key already in `_agent/.env` is loaded
into this shell only, and handed to onboarding:

```
set -a; source ~/Desktop/MIT/courses/_agent/.env; set +a
openclaw onboard --install-daemon --workspace ~/Desktop/MIT/courses \
  --auth-choice gemini-api-key --gemini-api-key "$GOOGLE_API_KEY"
```

Also give the key to the daemon itself (the LaunchAgent has no shell profile).
The line is copied without being printed:

```
grep '^GOOGLE_API_KEY=' ~/Desktop/MIT/courses/_agent/.env >> ~/.openclaw/.env
chmod 600 ~/.openclaw/.env
```

**Why Gemini Flash.** Pro previews have no free tier, and Flash does.
`google/gemini-3.8-flash` is set in `openclaw.json5`. Things to know about
the free tier:
- **Rate limits.** A brief takes about 10 model calls. If a run fails with
  429, re-run it; it's usually fine within a minute.
- **Your prompts may train Google's models.** Your assignment text and file
  names reach Google.
- **Weaker judgment.** Flash may produce a vaguer brief. It can't produce a
  broken one: `mitsync email` rejects an invalid brief, and the model retries.

**4. Config.** Merge `_agent/openclaw/openclaw.json5` into
`~/.openclaw/openclaw.json`: the workspace, `model.primary`, heartbeat off,
and exec in allowlist mode. Then restart the gateway and check:

```
openclaw models list --provider google        # confirm the model id exists; fix it in the config if not
openclaw approvals allowlist add ~/Desktop/MIT/courses/_agent/bin/mitsync-agent
openclaw gateway restart
openclaw doctor && openclaw skills list       # mit-briefing should be "eligible"
```

**5. Gmail app password (the delivery).** The brief is sent from your Gmail
to your Gmail (`email.sender` and `email.to` in `config/settings.yml`). Gmail
won't accept your real password for this; it needs an *app password*:

1. Google Account → Security → turn on **2-Step Verification** (required).
2. Go to myaccount.google.com/apppasswords → create one called "mitsync".
3. Add it to `_agent/.env`: `GMAIL_APP_PASSWORD=abcdabcdabcdabcd` (no spaces).

The renderer is already set up: `email/node_modules` is installed, and
`email.node` points at Node 26 by absolute path.

**6. macOS permissions (the usual snag).** The gateway runs as a different
process from your Terminal, and both `~/Desktop` and Calendar are
privacy-protected. In System Settings → Privacy & Security → **Full Disk
Access**, add the `node` binary the gateway runs. Find its path with
`openclaw gateway status --deep`; it's usually
`~/.nvm/versions/node/v26.10.0/bin/node`. Approve the Calendar prompt the first
time it appears. If you skip Calendar, the brief still works and says
"calendar unavailable".

**7. Test the tools without the agent.**

```
cd ~/Desktop/MIT/courses/_agent
uv run mitsync doctor                    # every row ok (calendar may warn)
bin/mitsync-agent due --days 7 --json | head -40
bin/mitsync-agent organize apply --plan x   # must be REFUSED by the wrapper
```

**8. First agent run, interactively.**

```
openclaw dashboard         # web chat at http://127.0.0.1:18789
```

Type `/mit-briefing`. You'll see the tool calls (`sync`, `due`, `work`, file
reads, the brief JSON being written, `email --dry-run`, `email`). Then the
dashboard lands in your inbox. **Screenshot the email next to the tool
trace** for the report.

**9. Schedule it.**

```
openclaw cron add "30 7 * * 1-5" "Use the mit-briefing skill." \
  --name morning-brief --tz America/New_York --session isolated \
  --no-deliver          # the skill emails the brief itself
openclaw cron list                         # note the job id
openclaw cron run <id> --wait              # test now; prints "already sent" if step 8 sent today's
openclaw cron runs <id>                    # history
```

The Mac must be awake at 07:30, because a sleeping laptop skips the run. To
wake it a minute early:

```
sudo pmset repeat wakeorpoweron MTWRF 07:29:00
```

## When something breaks

| symptom | fix |
|---|---|
| `openclaw: command not found` | nvm isn't on Node 26 in this shell: `nvm use 26` |
| model auth error / 401 | `GOOGLE_API_KEY` missing from `~/.openclaw/.env`; step 3, then `openclaw gateway restart` |
| 429 / quota exceeded | free-tier rate limit; wait a minute and re-run, or `openclaw cron run <id>` |
| "unknown model" | `openclaw models list --provider google`, and set that id in `model.primary` |
| exec "denied" / "not allowlisted" | the command wasn't `…/_agent/bin/mitsync-agent`; redo the allowlist in step 4 |
| sync finds nothing / files missing | Full Disk Access for the gateway's `node` (step 6) |
| "calendar unavailable" in the brief | Calendar permission (step 6); everything else still works |
| no email | `uv run mitsync email --dry-run` names the problem: no brief JSON, a schema error, no `GMAIL_APP_PASSWORD`, or a Node path. `state/sent/<date>.json` means today's was already sent (`--resend`) |
| Gmail "Username and Password not accepted" | the password is your real one or has spaces; make an app password (step 5) |
| anything else | `openclaw doctor`, `openclaw logs --follow`, `openclaw status --deep` |

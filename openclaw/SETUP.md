# OpenClaw for mitsync: what it is, and how to set it up

Facts below are from docs.openclaw.ai as of 2026-09. OpenClaw moves fast, so
when this file and `openclaw <cmd> --help` disagree, trust `--help`.

## What OpenClaw is, in one picture

```
 you (web dashboard / terminal)                      cron: "30 7 * * *"
                 │                                        │
                 ▼                                        ▼
        ┌──────────────── Gateway (always-on daemon, port 18789) ────────────┐
        │  receives messages & timers → runs an agent turn → sends reply     │
        │                                                                    │
        │  agent loop:  system prompt = AGENTS.md + SOUL.md + USER.md        │
        │               + skill list (name + description of each SKILL.md)   │
        │     model (Claude) ──► picks a tool ──► exec / read / write ──┐    │
        │         ▲                                                    │    │
        │         └──────────── tool result ◄─────────────────────────┘    │
        └────────────────────────────────────────────────────────────────────┘
                 │ exec (allowlisted)
                 ▼
        _agent/bin/mitsync-agent sync | due | work | email …  ──► Gmail (SMTP) ──► your inbox
```

- **Gateway**: a background process (a macOS LaunchAgent, `ai.openclaw.gateway`)
  that stays up, so the agent can act at 07:30 when no terminal is open. It
  hosts the web dashboard at http://127.0.0.1:18789.
- **Agent loop**: each turn, the model sees the bootstrap files, the list of
  skills, and the conversation. It calls tools (shell `exec`, file read and
  write, `memory_search`) and loops until it can answer.
- **Skills**: folders with a `SKILL.md`. Only the name and description sit in
  the prompt. When one matches, the model reads the full file and follows it.
  Each skill is also a slash command (`/mit-briefing`).
- **Cron**: scheduled prompts. At the scheduled time the gateway starts an
  *isolated* session with your message ("use mit-briefing"), and can deliver
  the reply to a channel.
- **Channels**: chat apps OpenClaw can reply through (Telegram, WhatsApp, …). This setup uses none: the brief arrives by email, sent by `mitsync email`.
  Not used here: the brief is a *tool* output. `mitsync email` renders a
  dashboard and sends it from your Gmail to your Gmail.
- **Memory**: plain Markdown in the workspace (`MEMORY.md`, `memory/<date>.md`),
  searchable with `memory_search`. Used lightly here; it's the next homework.

## Where the files live

```
~/.openclaw/                   OpenClaw's own state (never in git)
  openclaw.json                config (JSON5). Merge ../openclaw/openclaw.json5 into it
  .env                         optional secrets for OpenClaw itself
  state/openclaw.sqlite        cron jobs, exec allowlist, shared state
  agents/main/…                sessions, transcripts, model auth
  credentials/                 channel logins

~/Desktop/MIT/courses/         the agent WORKSPACE (agents.defaults.workspace)
  AGENTS.md  -> _agent/openclaw/workspace/AGENTS.md   operating rules, loaded every turn
  SOUL.md    -> _agent/openclaw/workspace/SOUL.md     tone
  USER.md    -> _agent/openclaw/workspace/USER.md     who you are
  skills     -> _agent/skills                         mit-briefing, mit-canvas-sync, …
  IDENTITY.md, memory/        created by OpenClaw itself
  Machine Learning/ …  _canvas/  _kb/  _agent/        what the agent works on
```

The symlinks keep everything that defines the agent's behaviour under git in
`_agent/`. mitsync only indexes the course folders listed in
`config/courses.yml`, so OpenClaw's own files never end up in the knowledge base.

## Setup, step by step (about 20 minutes)

**0. Node.** OpenClaw needs Node 24.16+ or 26.1+, and you have 20.19 (nvm):

```
nvm install 26 && nvm alias default 26 && node --version
```

**1. Install and onboard.** Onboarding sets up model access first, then installs the gateway daemon:

```
npm install -g openclaw@latest
openclaw onboard --install-daemon --workspace ~/Desktop/MIT/courses
```

For the model, pick **Anthropic**. You have two options:
- an API key (`ANTHROPIC_API_KEY`). The docs recommend this for automation.
- your Claude subscription: `claude setup-token`, then
  `openclaw models auth login --provider anthropic --method setup-token`. This
  draws on your plan's usage limits.

**2. Link the agent's files into the workspace.** Do this before the first
chat, so OpenClaw doesn't write its default templates:

```
cd ~/Desktop/MIT/courses
for f in AGENTS SOUL USER; do ln -sf _agent/openclaw/workspace/$f.md $f.md; done
ls -l skills        # should already be: skills -> …/_agent/skills
```

If onboarding already wrote real `AGENTS.md`/`SOUL.md`/`USER.md` files there,
look at them, then replace them with the links.

**3. Config.** Merge `_agent/openclaw/openclaw.json5` into
`~/.openclaw/openclaw.json` (or set each key with `openclaw config set`). Then:

```
openclaw approvals allowlist add ~/Desktop/MIT/courses/_agent/bin/mitsync-agent
openclaw doctor && openclaw skills list      # mit-briefing should be "eligible"
```

No secret goes into OpenClaw for Canvas: mitsync reads `CANVAS_TOKEN` from
`_agent/.env` itself, whoever runs it.

**4. macOS permissions (the usual snag).** The gateway runs as a different app
from your Terminal, and `~/Desktop` and Calendar are privacy-protected. In System Settings → Privacy &
Security, give the `node` binary that runs the gateway **Full Disk Access** (see
`openclaw gateway status --deep` for the path). The first time the agent reads
the calendar, approve the Calendar prompt. If you skip this, the brief still
works and says "Calendar unavailable".

**5. First run, interactively.**

```
openclaw dashboard         # web chat at http://127.0.0.1:18789
```

Type: `/mit-briefing`. You'll see the tool calls (`mitsync-agent sync`,
`due`, `work`, file reads, `email`) and then the dashboard in your inbox.
**Screenshot the email next to the dashboard's tool trace.**

**6. Email (the delivery).** The brief is sent from your Gmail to your
Gmail (`email.sender` / `email.to` in `config/settings.yml`). Gmail needs an
*app password* for this, not your real one:

1. Google Account → Security → turn on 2-Step Verification (required).
2. Google Account → Security → **App passwords** → create one called "mitsync".
3. Put it in `_agent/.env` (gitignored): `GMAIL_APP_PASSWORD=abcdabcdabcdabcd`
4. Build the renderer once, and tell mitsync which Node to use (a LaunchAgent has no nvm):

```
cd ~/Desktop/MIT/courses/_agent/email && npm install
command -v node        # paste this path into config/settings.yml → email.node
cd .. && uv run mitsync email --dry-run    # needs a brief JSON; open the .html it writes
```

**7. Schedule it.**

```
openclaw cron add "30 7 * * 1-5" "Use the mit-briefing skill and send me the result." \
  --name morning-brief --tz America/New_York --session isolated \
  --no-deliver          # the skill emails the brief itself
openclaw cron list                         # note the job id
openclaw cron run <id> --wait              # test it now, without waiting for 07:30
openclaw cron runs <id>                    # history
```

## When something breaks

| symptom | fix |
|---|---|
| `openclaw: command not found` in a new shell | nvm isn't loaded; `nvm use 26` or fix `~/.zshrc` |
| exec "denied" / "not allowlisted" | the command wasn't `…/_agent/bin/mitsync-agent`; re-run step 3 |
| brief says files are missing / sync finds nothing | Full Disk Access for the gateway's `node` (step 4) |
| "Calendar unavailable" | Calendar permission for the gateway (step 4); the brief still works |
| exit code 20 in a transcript | not an error: the agent is resolving a judgment task (see `_agent/CLAUDE.md`) |
| no email | `uv run mitsync email --dry-run` names the problem (missing brief JSON, schema error, no `GMAIL_APP_PASSWORD`, Node path); `state/sent/<date>.json` means it was already sent today |
| anything else | `openclaw doctor`, `openclaw logs --follow`, `openclaw status --deep` |

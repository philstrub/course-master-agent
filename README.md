# mitsync

An agent that keeps my MIT courses under control. Every morning it tells me
what is due, how far along I really am on each homework, and what to review to
finish it. It mirrors Canvas, files material into my own folders, and reads
(never writes) Apple Calendar.

> **Deterministic Python does the I/O. The agent's model does the judgment.**
> The CLI never calls a model. OpenClaw (on a schedule) or Claude Code (in a
> terminal) drives it and brings the reasoning.

```
cron 07:30 / terminal
        │
        ▼
  agent (OpenClaw or Claude Code) ── reads skills/*/SKILL.md
        │  exec: facts                   │ file reads
        ▼                                ▼
  mitsync sync · due · work · unfiled    _kb/ text, my drafts, handouts
        │                                │
        ▼                                ▼ writes its judgment as JSON
  Canvas (GET only) · Apple Calendar    _kb/briefings/<date>-morning.json
  (read-only) · _canvas/ mirror                 │
                                               ▼
                               mitsync email ── validate, render, send once ──▶ my inbox
```

## Quickstart

```bash
uv sync --extra dev
cp .env.example .env          # add CANVAS_TOKEN and GMAIL_APP_PASSWORD
(cd email && npm install)     # the React Email dashboard renderer
uv run mitsync doctor         # what works, what is missing
uv run mitsync sync           # mirror Canvas (incremental)
uv run mitsync due            # what is due, with my own submission status
```

Then ask an agent for the morning brief. It follows
`skills/mit-briefing/SKILL.md`, writes `_kb/briefings/<today>-morning.json`,
and runs `mitsync email`, which sends the dashboard to the address in
`config/settings.yml`.

## Where to read next

| file | what |
|---|---|
| `docs/TOOLS_AND_SKILLS.md` | **Every command and skill on one page**, and OpenClaw vs. Claude Code |
| `openclaw/SETUP.md` | What OpenClaw is, its files, and step-by-step setup |
| `CLAUDE.md` | The contract for any agent driving this repo: data tools, guardrails |
| `config/naming.md` | Filing rules, in prose; `unfiled --json` hands them to the agent |
| `config/courses.yml` | Canvas course id → my folder name. The only folders ever indexed |
| `AGENTS.md` | House code style |
| `docs/API_NOTES.md` | Canvas and EventKit behaviour observed in practice |

## Layout

```
~/Desktop/MIT/courses/        the workspace (and OpenClaw's)
  Machine Learning/ …         my folders; never moved without an approved plan
  _canvas/                    verbatim Canvas mirror; source of truth
  _kb/                        AGENTS.md, graph/, text/, briefings/, courses/<Course>/{COURSE.md,readings.json}
  _agent/                     this repo
    bin/mitsync-agent         the one binary OpenClaw may execute
    mitsync/                  core · canvas · filing · schedule · knowledge · cli
    email/                    React Email dashboard (shadcn-style components) + brief schema
    calendar-helper/          MitsyncCalendar.app: read-only EventKit, holds the gateway's Calendar grant
    skills/                   mit-briefing · mit-canvas-sync · mit-organize · mit-graph-query · mit-graph-build · mit-course
    openclaw/                 openclaw.json5 + workspace/{AGENTS,SOUL,USER}.md
```

`make check` runs lint and the tests. No test needs a network, a key, or a model.

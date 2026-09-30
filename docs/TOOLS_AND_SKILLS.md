# Tools and skills

**Tools do the I/O and the checks. The agent's model makes every judgment.**
The CLI never calls a model. Whichever agent drives it (OpenClaw or Claude
Code) reads the tools' JSON, decides, and hands its decisions back as files.

## Tools: the `mitsync` CLI

The agent reaches it through `_agent/bin/mitsync-agent` (OpenClaw) or
`uv run mitsync` (Claude Code). Every read command takes `--json`.

| command | what it does | touches |
|---|---|---|
| `sync` | mirror new Canvas files and metadata (assignments, your own submission status) into `_canvas/` | Canvas **read-only** (GET/HEAD, enforced in code and tests) |
| `due` | deadlines with your status and the assignment text; `_kb/due.json` | reads the mirror |
| `work` | your files per course, tagged `canvas_copy` / `edited` / `yours` | reads your folders |
| `calendar` | Apple Calendar events | Calendar **read-only** |
| `unfiled` | mirror files not yet filed, plus the rules and schema a plan must follow | reads |
| `organize apply --plan P` · `undo` | validate an agent-written plan, confirm, file by hardlink, keep an undo log | your folders: the agent may apply Canvas copies (`--yes`); `--include-existing` and `undo` are **human only** |
| `extract` | PDFs/notebooks → text in `_kb/text/` (incremental, by sha256) | `_kb/` |
| `kb build` | per-course `INDEX.md`, `manifest.json`, `_kb/AGENTS.md`; never `NOTES.md` | `_kb/` |
| `graph add F` · `rebuild` · `query` | append agent-written facts (checked against the ontology, all or nothing), rebuild the DuckDB cache, query it | `_kb/graph/` |
| `graph push` · `cypher Q` | replace the Neo4j projection with the live graph, run read-only Cypher on it | Neo4j (`NEO4J_*` in `.env`) |
| `email [--dry-run]` | validate the agent's brief JSON, add today's calendar and sync freshness, render the dashboard, send it **once per day** to the address in config | Gmail SMTP |
| `doctor` | what is configured and what is missing | — |

**How a judgment gets back into the system.** As data, and always checked:
the brief is a JSON file checked against `email/brief.schema.json`; a filing
plan is checked by `organize apply`; graph facts are checked by `graph add`.
A bad file is rejected, with the failing field named, and the agent fixes it.
Nothing the agent writes can choose the email recipient or move one of your own files.

## Skills: `_agent/skills/*/SKILL.md`

A skill is a prompt the agent loads when your request matches its description.
It says which tools to run and what to judge.

| skill | you say | tools | the judgment |
|---|---|---|---|
| **`mit-briefing`** | "morning brief", "am I behind?", cron 07:00 | `sync`, `due`, `work`, then `email` | how far along each homework is (handout parts vs. your drafts), 1–3 things to review, hours left |
| `mit-canvas-sync` | "anything new on Canvas?" | `sync` | which errors are expected (hidden Files tab, throttling) and which are real (expired token) |
| `mit-organize` | "file my new material", cron every 2 h 08–22 | `unfiled`, `organize apply --yes` | where each file goes, per `config/naming.md`; files Canvas copies, never your own files |
| `mit-kb` | "where is X taught?" | `extract`, `kb build`, `graph add/query` | course notes and concept facts; parked for the memory homework |

## OpenClaw vs. Claude Code driving the same tools

| | **OpenClaw** (autonomous) | **Claude Code** (interactive) |
|---|---|---|
| starts a turn | cron (brief 07:00, filing every 2 h 08–22), or a dashboard chat message | you, in the terminal |
| instructions | workspace `AGENTS.md` + `SOUL.md` + `USER.md` (copied from `_agent/openclaw/workspace/` by `make openclaw-workspace`) | `_agent/CLAUDE.md` |
| skills | discovered from `<workspace>/skills`, also slash commands | the same files, read on request |
| shell | allowlisted to **one binary**, `mitsync-agent`, which refuses `organize undo` and `--include-existing` | any command, behind Claude Code's permission prompts |
| output | the emailed dashboard, plus `_kb/briefings/<date>-morning.{json,html}` | terminal, and the same files |
| memory between runs | files on disk, plus OpenClaw `memory/<date>.md` | files on disk |

The tools and skills are identical. Only the trigger and the sandbox differ,
which is why the CLI holds no judgment.

# Tools and skills

**Tools do the I/O and the checks. The agent's model makes every judgment.**
The CLI never calls a model. Whichever agent drives it (OpenClaw or Claude
Code) reads the tools' JSON, decides, and hands its decisions back as files.

## Tools: the `mitsync` CLI

The agent reaches it through `_agent/bin/mitsync-agent` (OpenClaw) or
`uv run mitsync` (Claude Code). Every read command takes `--json`.

| command | what it does | touches |
|---|---|---|
| `sync` | mirror new Canvas files and metadata (assignments, your own submission status) into `_canvas/`, plus the Google Slides decks and HBS cases that modules link to | Canvas **read-only** (GET/HEAD, enforced in code and tests). HBS cases: the LTI launch POST to HBS only |
| `gradescope sync` | snapshot the student's Gradescope status and scores; `due` then shows them on the matching Canvas row (Canvas reports Gradescope work as unsubmitted) | Gradescope **read-only**, `state/gradescope.json` |
| `due` | deadlines with your status and the assignment text, plus each required reading once it is filed (from `readings.json`); cached in `state/due.json` | reads the mirror |
| `work` | your files per course, tagged `canvas_copy` / `edited` / `yours` | reads your folders |
| `calendar` | Apple Calendar events | Calendar **read-only** |
| `unfiled` | mirror files neither filed nor skipped, plus the rules and schema a plan must follow and any case link sync could not download | reads |
| `organize apply --plan P` · `undo` | validate an agent-written plan, confirm, file by hardlink, keep an undo log | your folders: the agent may apply Canvas copies (`--yes`); `--include-existing` and `undo` are **human only** |
| `extract` | PDFs/notebooks → text in `_kb/text/` (incremental, by sha256) | `_kb/` |
| `kb build` | graph backbone from the course folders, and `_kb/AGENTS.md`. Never writes a `COURSE.md` | `_kb/` |
| `kb check` | per course: documents the master file `COURSE.md` does not cover yet (new or changed), sources gone, and `readings.json` problems | reads |
| `graph add F` · `rebuild` · `query` | append agent-written facts (checked against the ontology, all or nothing), rebuild the DuckDB cache, query it (`--param NAME=VALUE`, `--json`) | `_kb/graph/` |
| `graph check` | what the graph still lacks: Canvas files not yet filed, lectures to number, items without concepts, files without a parent | reads |
| `graph push` · `cypher Q` | replace the Neo4j projection with the live graph, run read-only Cypher on it | Neo4j (`NEO4J_*` in `.env`) |
| `email [--dry-run]` | validate the agent's brief JSON, add today's calendar, the files new on Canvas since the last brief, and sync freshness, render the dashboard, send it **once per day** to the address in config | Gmail SMTP |
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
| `mit-organize` | "file my new material", cron every 2 h 08–22 | `unfiled`, `organize apply --yes` | where each file goes and its per-course name, per `config/naming.md`; files Canvas copies, never your own files |
| `mit-graph-query` | "where is X taught?", "what's in lecture 5?", "what did I submit?" | `graph query`, `graph cypher` (read-only) | which query answers the question, and what the returned `text` pages say |
| `mit-graph-build` | "rebuild / complete the knowledge base" | `extract`, `kb build`, `graph check/add` | lecture numbers, concepts and file parents, until `graph check` is clean |
| `mit-course` | "update the course notes", cron nightly | `extract`, `kb build`, `kb check` | each course's master file `COURSE.md` (what every lecture, recitation, assignment and reading says) and its `readings.json`, until `kb check` is clean |

## OpenClaw vs. Claude Code driving the same tools

| | **OpenClaw** (autonomous) | **Claude Code** (interactive) |
|---|---|---|
| starts a turn | cron (brief 07:00, filing every 2 h 08–22, master files 23:00), or a dashboard chat message | you, in the terminal |
| instructions | workspace `AGENTS.md` + `SOUL.md` + `USER.md` (copied from `_agent/openclaw/workspace/` by `make openclaw-workspace`) | `_agent/CLAUDE.md` |
| skills | discovered from `<workspace>/skills`, also slash commands | the same files, read on request |
| shell | allowlisted to **one binary**, `mitsync-agent`, which refuses `organize undo` and `--include-existing` | any command, behind Claude Code's permission prompts |
| output | the emailed dashboard, plus `_kb/briefings/<date>-morning.{json,html}` | terminal, and the same files |
| memory between runs | files on disk, plus OpenClaw `memory/<date>.md` | files on disk |

The tools and skills are identical. Only the trigger and the sandbox differ,
which is why the CLI holds no judgment.

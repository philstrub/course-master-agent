# mitsync

`mitsync` is a CLI that mirrors an MIT student's Canvas LMS course materials
into a local, agent-readable knowledge base, files new materials into the
student's own folder scheme, reads (never writes) Apple Calendar for class
times, and maintains an append-only knowledge graph of course content. It is
built to be driven either by a cloud LLM API or by whatever agent (Claude
Code, OpenClaw, a chatbot) is currently sitting in the terminal — with
identical on-disk results either way.

See `docs/PRD.md` for the problem statement and requirements, and
`CLAUDE.md` for the guardrails and contract an agent driving this repo must
follow.

## What it does

1. **Sync** — incrementally mirrors `canvas.mit.edu` course files, pages,
   modules, and announcements into `_canvas/<course>/` plus a DuckDB
   manifest (`state/manifest.duckdb`).
2. **Organize** — proposes filing newly synced materials into the student's
   own course folders (`Machine Learning/`, `Optimization/`, etc.), using a
   prose rules file (`config/naming.md`) and an id→folder map
   (`config/courses.yml`). Additive only: pre-existing files are never moved
   without an explicit dry-run plan the user approves, and every apply
   writes an undo log (`state/undo/`).
3. **Calendar** — reads Apple Calendar read-only via an EventKit CLI
   (`ical-guy`) to find class meeting times. No calendar writes, ever.
4. **Knowledge base** — extracts text, writes per-course markdown notes, and
   builds an ontology-driven knowledge graph. The canonical graph is
   append-only JSONL (`_kb/graph/triples.jsonl`, `_kb/graph/nodes.jsonl`),
   projected into a rebuildable DuckDB store (`state/graph.duckdb`) for
   querying. A Neo4j/Cypher backend is a possible future implementation of
   the same interface.
5. **Optional hosting** — can run under OpenClaw for scheduled syncs and
   chat, but OpenClaw is never required; every command runs standalone.

## Quickstart

### Mode A — with a cloud API key (`--driver api`)

```
export MITSYNC_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-...
mitsync sync                          # pure I/O: no driver
mitsync organize plan --driver api
mitsync organize apply --yes          # only after reviewing the plan
mitsync kb build --driver api
```

Any OpenAI-compatible provider works by setting `MITSYNC_LLM_PROVIDER` and
`MITSYNC_LLM_BASE_URL` in `config/settings.yml` or the environment; see
`docs/ENGINEERING_PLAN.md` for the driver table.

### Mode B — with an agent in the loop, zero credentials (`--driver agent`, the default)

```
mitsync organize plan --driver agent
# -> writes state/tasks/<uuid>.json, prints its path, exits 20
# the driving agent reads the task file, reasons about it, and writes a result
mitsync resolve state/tasks/<uuid>.json --result /tmp/result.json
```

`resolve` validates the result against the task's JSON schema and applies
it deterministically — this is how the whole tool works with no API key at
all, driven by whatever agent is already running (Claude Code, OpenClaw, a
chatbot pasting JSON back).

### Mode C — deterministic only, for CI and dry runs

```
mitsync organize plan --driver rules
```

## Command table

| Command                    | Purpose                                              | Judgment used |
|-----------------------------|-------------------------------------------------------|---------------|
| `mitsync sync`              | Incrementally mirror Canvas into `_canvas/<course>/`  | none (I/O)    |
| `mitsync map`               | Resolve Canvas course ids to folder names             | classification |
| `mitsync organize plan`     | Propose a filing plan for newly synced material        | classification/naming |
| `mitsync organize apply`    | Apply an approved plan, writing an undo log            | none (I/O)    |
| `mitsync organize undo`     | Reverse the last (or a named) apply                    | none (I/O)    |
| `mitsync calendar`          | Read class times from Apple Calendar (read-only)       | none (I/O)    |
| `mitsync due`               | List upcoming deadlines across courses                 | none (I/O)    |
| `mitsync brief`             | Generate a "what's due / what to read" briefing        | synthesis     |
| `mitsync extract`           | Extract text from synced documents                     | none (I/O), OCR/parsing only |
| `mitsync graph rebuild`     | Rebuild `state/graph.duckdb` from the JSONL graph       | none (I/O)    |
| `mitsync graph query`       | Query the knowledge graph                              | none (I/O)    |
| `mitsync kb build`          | Write/update `_kb/` notes, index, and graph extraction  | note-writing, graph extraction |
| `mitsync resolve`           | Apply a driving agent's result for a pending task file | none (validation + I/O) |
| `mitsync doctor`            | Diagnose manifest/graph/Canvas/calendar health          | none (I/O)    |

Four commands take `--driver {api,agent,rules}` and `--resolve`, because only
they need judgment: `map`, `organize plan`, `graph extract`, and `kb build`.
Every other command is pure I/O and never produces a pending task. `rules` and

## Folder contract

```
~/Desktop/MIT/courses/
  _canvas/<course>/           verbatim Canvas mirror + _meta/*.json
  Machine Learning/ ...       student's own curated folders (populated by
                               hardlink/copy from _canvas/, never the only
                               copy of anything)
  _kb/
    INDEX.md                  human-facing map of the knowledge base
    AGENTS.md                 entry point for a downstream agent (see PRD)
    due.json                  machine-readable deadline list
    briefings/                generated per-week/per-course briefings
    courses/<course>/NOTES.md per-course markdown notes
    text/                     extracted plain text per source document
    graph/                    triples.jsonl, nodes.jsonl (canonical graph)
  _agent/
    state/
      manifest.duckdb          Canvas sync manifest
      graph.duckdb             rebuildable projection of _kb/graph/*.jsonl
      tasks/                   pending agent-driver task files
      undo/                    undo logs for organize apply
```

`AI_Studio/nandatown` and any `.venv`/`site-packages`/`node_modules` are
never touched, synced, indexed, or walked by any command.

## Docs

- `docs/PRD.md` — problem, users, goals, non-goals, requirements, acceptance criteria, risks
- `docs/ENGINEERING_PLAN.md` — architecture, module contracts, data model, error taxonomy, phases, test plan
- `docs/API_NOTES.md` — Canvas API, Apple Calendar/EventKit, and OpenClaw research (with unverified items marked)
- `docs/RUNBOOK.md` — setup, daily operation, failure modes
- `docs/ADR/` — architecture decision records
- `CLAUDE.md` / `AGENTS.md` — guardrails and contract for an agent driving this repo

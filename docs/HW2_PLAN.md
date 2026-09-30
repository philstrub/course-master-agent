# HW2 engineering plan: a knowledge-graph memory with a verified sync loop

Homework 2 ("Engineer a Reliable Agent", `assignment2.md`) asks for tool use,
persistent memory, an observable loop with a stopping condition and an
escalation, a bounded subagent handoff, one failure-and-recovery, and a 3-5
case baseline-vs-improved evaluation. This plan maps each rubric line onto
concrete changes to `mitsync` and the OpenClaw / Claude Code configuration,
in the order they will be built. Evaluation and written deliverables are
listed last and are deliberately not started yet.

## 0. Baseline: what is wrong today (measured 2026-09-30)

These are the defects the improved configuration must fix. They double as the
evaluation's baseline numbers.

| defect | evidence |
|---|---|
| one course becomes two Course nodes | `_kb/graph/nodes.jsonl` has 13 Course nodes for 7 courses: every `_canvas/<Canvas name>/...` path is its own "course" (`extract.course_of`) |
| the same file is two nodes | a filed hardlink and its `_canvas/` original get separate `Resource` ids (id = hash of the path, not the content) |
| the graph knows nothing about structure | only `Resource -part_of-> Course`; no lecture, recitation, assignment or syllabus nodes, so "what files belong to HW2?" is unanswerable |
| unsupported files are invisible | `.docx`, `.pptx`, `.zip`, code are never in the graph (backbone reads `_kb/text/` only) |
| no memory of submitted work | Canvas submission state lives in `due --json` for one run and is never stored |
| no completeness signal | nothing says "the graph is done"; nothing an agent loop could stop on |
| module-only material missed | content linked only from Canvas *modules* (e.g. AI Studio assignments) is not mirrored; `_canvas/<course>/_meta/modules.json` is fetched but its file/page items are not downloaded |
| Gradescope submissions unknown | assignments submitted through Gradescope show as `unsubmitted` in Canvas |
| naming imprecision | loose pre-existing files at course roots (`Machine Learning/Lec1.pdf`), duplicate item folders (`recitation 1/` beside `recitations/recitation-01/`) |

## 1. Architecture

```
            goal (cron / human prompt)
                     |
              +------v-------+      read-only CLI tools (mitsync --json)
              |  main agent  |----> sync, extract, unfiled, graph backbone,
              | (OpenClaw)   |      graph add, graph check, conventions
              +--+--------+--+
     bounded     |        |    bounded
     handoff     |        |    handoff
          +------v--+  +--v-----------+
          | verifier|  | brief writer |   (fresh context, one JSON out,
          | subagent|  | subagent     |    validated by a command)
          +------+--+  +--+-----------+
                 |        |
          memory / state (all on disk, all rebuildable except the JSONL truth)
          _kb/graph/*.jsonl  -> DuckDB (local)  -> Neo4j Aura (display + Cypher)
          state/loop/<run>.jsonl  (loop trace)   memory/*.md (OpenClaw notes)
```

The CLI stays a data tool: it never calls a model. Every judgment is a file
the agent writes and a command validates (the existing contract in
`CLAUDE.md`).

## 2. Workstreams, in build order

### W1. Ontology as pydantic models (rubric: memory, failure detection)  — DONE

- `mitsync/knowledge/ontology.py` replaces `config/ontology.yml`. Node
  types: `Course`, `Syllabus`, `Lecture`, `Recitation`, `Assignment`,
  `File` (documents), `DataFile` (data and code), `Repo`, `Concept`. Edge types:
  `course_follows_syllabus`, `lecture_of_course`, `assignment_of_course`,
  `recitation_of_course`, `file_of_lecture`, `file_of_assignment`,
  `file_of_recitation`, `file_of_syllabus`, `file_of_course`,
  `concept_in_lecture`, `concept_in_assignment`, `concept_in_recitation`,
  `repo_of_assignment`, `repo_of_course`.
- Each node type is a pydantic model of its `attrs` (`extra="forbid"`) with a
  typed id prefix (`lecture:<course-slug>:<NN>`); each edge type declares the
  node types it may join. A bad record raises `OntologyError` naming it.
- Structural rules live beside the types: every file has exactly one parent
  edge, every lecture/recitation/assignment exactly one course, every concept
  at least one occurrence, `file_of_course` / `repo_of_course` carry a
  mandatory `reason` and are rationed per course, a file inside
  `assignments/hw-01/` must hang off `assignment:<course>:hw-01`, and a file
  whose content exists at two course-folder paths is a duplicate.
- `mitsync graph schema --json` prints the ontology for any agent (Claude Code
  in `/courses`, OpenClaw) so no one reads the Python.

### W2. Deterministic backbone v2 (rubric: tool use, memory)  — DONE

- Course nodes come from `config/courses.yml`; `_canvas/<Canvas name>/` maps
  back to its workspace folder through the course number. One course, one node.
- File nodes are keyed by content hash: the filed hardlink and its mirror
  original are one node (`path` + `mirror_path`). Same content at two course
  paths is recorded in `duplicates` and flagged.
- The folder a file is filed in decides its parent when it is unambiguous
  (`assignments/<item>/`, `recitations/<item>/`, `syllabus/`, numbered
  lecture files in `lectures/`, `other/`, `notes/`). Everything else is left
  unattached, which `graph check` reports as `unfiled`: that is the agent's
  work list.
- Walks every document and data extension on disk, not only extractable text.
- Measured on the real workspace (2026-09-30): 1 Course node per course
  (was 13), 229 files -> 274 nodes / 127 edges, 166 `unfiled` for the agent
  (82 of them in `Analytics Edge/Assignment 1/`), 48 `duplicate_content` for
  the human (a loose pre-existing original beside its filed copy).
- TODO: `kb.inventory` still groups by `extract.course_of`, so `kb build`
  reports 13 courses; switch it to `mirror_course_folders`.

### W3. `mitsync graph check` as the loop's stopping oracle (rubric: loop, failure detection)  — graph check DONE, conventions TODO

- `graph check --json` returns every structural violation with a stable code
  (`unfiled`, `multiple_parents`, `no_course`, `orphan_concept`,
  `misc_overuse`, `wrong_folder`, `course_mismatch`, `duplicate_content`),
  exit 1 when any exist.
- `mitsync conventions --json`: filesystem naming checks from
  `config/naming.md` §2 (per-item folders only, `recitation-NN` / `hw-NN`,
  no case-variant duplicates of a bucket, no loose files in
  `recitations/` or `assignments/`). Pre-existing violations are reported
  with `needs_human: true`, because guardrail 1 forbids fixing them unasked.

### W4. Repair the inputs (rubric: tool use)

- **Canvas modules.** Walk `modules.json` items of type `File`, `Page`,
  `Assignment`, `ExternalUrl`; download File items that are not in the Files
  tab (courses that hide Files still expose module items), render Page items
  to markdown under `_canvas/<course>/Modules/<module>/`. Record the module
  name in the manifest so filing (naming.md §5: "module grouping wins") can
  use it. Read-only; `test_canvas_read_only.py` keeps holding.
- **Gradescope.** Canvas shows Gradescope-submitted work as `unsubmitted`.
  Option A (preferred, no new credentials): treat an assignment whose Canvas
  `submission_types` includes `external_tool` as Gradescope-backed, and
  derive `submission_status` from the student's own files on disk
  (`work --json` provenance: a student-authored PDF in the item folder newer
  than the handout) plus an explicit `mitsync submitted <assignment-id>`
  mark the human or agent writes. Option B: scrape Gradescope with the
  student's session cookie (read-only), which needs a secret and is brittle.
  **Decided: B, built** (`mitsync/gradescope/`, tests in
  `tests/test_gradescope.py`). Live run pending the cookie.
- Store submission state on `Assignment` nodes (`submission_status`,
  `submitted_at`, `score`, `submitted_via`) and mark the submitted file with
  `file_of_assignment {role: submission}`: this is the "memory of submitted
  work" other agents query.

### W5. Neo4j projection and tools (rubric: tool use) — built, live push pending

Built as `mitsync/knowledge/neo4j_store.py`, a second projection beside
DuckDB rather than a `GraphBackend` (the canned queries stay SQL).

- `Neo4jBackend` implementing `GraphBackend` over the bolt driver
  (`NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` from env / `.env`),
  modelled on the ledger `Neo4jServices`: uniqueness constraints per node
  label, `MERGE` on id, relationship type = edge name upper-cased.
- JSONL stays the only truth; Neo4j is a projection like DuckDB. `graph push`
  does a full idempotent re-projection (clear + load in batches) so Aura
  always mirrors the JSONL exactly. Display in Neo4j Aura's Explore view.
- Retrieval tools for other agents: `graph cypher "<query>"` (read-only:
  refuses `CREATE|MERGE|DELETE|SET|REMOVE|DROP`, runs in a READ transaction),
  and canned queries that work on either backend: `files_of <item>`,
  `concept <name>` (where it is taught / assessed), `submitted` (what the
  student turned in), `unfiled`.

### W6. The observable loop (rubric: loop, escalation)

- Skill `skills/mit-graph-sync/SKILL.md`: goal -> `sync` -> `extract` ->
  `graph backbone` -> `graph check --json` -> for each violation decide
  (attach an unfiled file, add concepts from the text, propose a filing plan)
  -> write a JSONL -> `graph add` -> `graph check` again.
- Stopping condition: `graph check` and `conventions` return zero violations
  that are not `needs_human`, and the verifier accepts. Hard cap: 5
  iterations. Stuck detector: the same violation surviving two iterations.
- Escalation (asks the human): an unreviewed filing plan (guardrail 1), a
  `needs_human` convention violation, the stuck detector, Neo4j or Canvas
  auth failure.
- Trace: every mitsync command run with `MITSYNC_LOOP_RUN=<id>` appends a
  line (`ts, command, exit, summary counts`) to `state/loop/<id>.jsonl`;
  `mitsync loop status` renders it as a live table. Together with
  `openclaw-watch` (tool-call trace) this is the demo's terminal view.
- Launch: `make graph-loop` -> `openclaw agent --message "run mit-graph-sync"`
  (and the same skill under `claude -p` in Claude Code).

### W7. The agentic team (rubric: delegation) — two subagents, no more

- **Verifier** (fresh context, cheap model): gets only `graph check --json`,
  `conventions --json`, the diff of JSONL lines appended this run, and the
  extracted text of a random sample of 5 touched files. Outputs a
  schema-validated verdict `{accept: bool, rejects: [{record, reason}]}`
  (`email/verdict.schema.json`), checked by `mitsync verdict check`. The main
  agent removes rejected records by appending corrections, then loops.
- **Brief writer**: already exists as the morning brief; moved behind a
  subagent handoff that receives `due --json` + `graph query submitted` and
  returns the brief JSON validated by `mitsync email --dry-run`.
- Cost: the backbone is deterministic, so the model only sees violations,
  not the whole tree. The verifier samples. No per-file subagents.

### W8. Skills for other agents (rubric: memory reuse)

- `skills/mit-kb/SKILL.md` rewritten against the new ontology;
  `skills/mit-graph-query/SKILL.md` for a study agent in `/courses`: "which
  lecture teaches X", "what did I submit for HW1", "what files do I need for
  recitation 3", with the exact commands. `_kb/AGENTS.md` points to it.

## 3. Later: evaluation and deliverables (not started)

- **Test cases** (same prompts on the HW1 config and the HW2 config):
  1. "File everything new from Canvas" -> measured: unfiled files, naming
     violations, duplicates after the run.
  2. "Which files do I need for AE HW1 and what did I submit?" (from a fresh
     Claude Code session in `/courses`) -> correct/incorrect, tool calls.
  3. "Where is logistic regression taught and assessed?" -> recall against a
     hand-labelled answer.
  4. Injected failure: a mis-filed file + a malformed graph record +
     Neo4j down -> detected? recovered or escalated? human interventions.
  5. Module-only AI Studio assignment -> present in graph or not.
- Measures: success rate, tool calls, wall-clock, token cost, human
  interventions.
- **Report** (PDF): task, configuration, architecture diagram, delegation
  trace, success trace, failure trace, results table, reflection, repro.
- **Video** (60-90 s): `make graph-loop` in one pane with the loop trace,
  Aura Explore in the other, then a Claude Code session answering a study
  question from the graph.

## 4. Decisions needed from the user

1. ~~Gradescope~~ Decided 2026-09-30: option B. `mitsync gradescope sync`
   reads the dashboard with `GRADESCOPE_COOKIE` (GET only, enforced), writes
   `state/gradescope.json`, and `due` plus the backbone's `Assignment` nodes
   take Gradescope's status. Open: the cookie is not in `.env` yet.
2. ~~Neo4j~~ Credentials added 2026-09-30. `graph push` / `graph cypher` are
   built. Open: `NEO4J_URI` is `localhost:7474` (the Browser's HTTP port) and
   no server is running. It needs `bolt://localhost:7687` after
   `make neo4j-up`, or an Aura `neo4j+s://` URI.
3. ~~Non-PDF documents~~ Decided 2026-09-30: `File` is any document a
   person reads (PDF, Word, PowerPoint, markdown, LaTeX), `DataFile` is data
   and code. `ontology.DOCUMENT_SUFFIXES` decides, and both models reject a
   path on the wrong side.

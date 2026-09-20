# PRD — mitsync

## Problem statement

An MIT student takes 7 courses (`Machine Learning`, `Analytics Edge`,
`Optimization`, `Analytics Tools`, `Analytics Lab`, `AI_Studio`,
`From Anaytics to Action`). Course materials live scattered across Canvas
(canvas.mit.edu) and the student's own hand-organized folders under
`~/Desktop/MIT/courses`. Today: nothing pulls Canvas material down
automatically; new files get filed manually (or not at all) into the
student's own naming scheme; nothing tracks what's due where; and there is
no structured, queryable knowledge base an agent could use to answer
course-specific questions without exploring the whole tree by hand. One
course folder (`AI_Studio/nandatown`) also contains a ~9.4k-file code repo
with a virtualenv that must never be treated as course material.

## Users

- **Primary user: the student.** Wants Canvas materials mirrored locally,
  filed into their own folders without disrupting what's already there,
  and a way to see what's due and what to read, without manually visiting
  Canvas or running any calendar-writing automation.
- **Secondary user (the one the design optimizes for): a downstream
  agent/chatbot.** The end goal is a knowledge base that another agent —
  pointed at this repo with no other context — can use to help the student
  with assignments: answering "what's due," locating the right document,
  and reasoning over course content without blind directory exploration.

## Goals

1. Incrementally mirror Canvas course files, pages, modules, and
   announcements into `_canvas/<course>/` with a durable manifest.
2. File newly synced material into the student's own folder scheme,
   governed by a tunable prose rules file and an id→folder map; additive
   only, dry-run first, always undoable.
3. Read Apple Calendar (read-only) for class meeting times.
4. Maintain a knowledge base: extracted text, per-course notes, and an
   ontology-driven knowledge graph (append-only JSONL canonical form,
   DuckDB projection for querying), usable by a downstream agent.
5. Work standalone with zero credentials (agent-driven judgment) or with a
   cloud API key (any OpenAI-compatible provider), with identical
   on-disk results either way.
6. Be optionally hostable by OpenClaw for scheduling/chat, without ever
   requiring it.

## Non-goals

- No writes to Canvas (no submissions, no posts, no grades). Enforced in code:
  `CanvasClient._request` refuses any method outside `{GET, HEAD}` with
  `CanvasWriteRefused`, and a test scans every module for HTTP write calls.
- No writes to Apple Calendar, ever, under any driver or mode.
- No re-hosting or redistribution of course material outside the student's
  own machine (no cloud storage, no sharing links, no third-party sync).
- Not a note-taking app — the student's own notes are not managed by this
  tool; `_kb/courses/<course>/NOTES.md` is generated/derived, not authored.
- Not a Canvas replacement or LMS UI — no assignment submission, no
  discussion participation, no grade viewing beyond what feeds `due`/`brief`.
- No processing of `AI_Studio/nandatown` or any `.venv`/`site-packages`/
  `node_modules` directory, anywhere.

## Functional requirements

| ID | Requirement | Phase | Acceptance criterion |
|----|---|---|---|
| FR-1 | Authenticate to Canvas via a student-supplied personal access token; validate it before use | 1 | `mitsync doctor` reports token validity via a live, low-cost Canvas call |
| FR-2 | Enumerate active courses for the current term, filtering client-side on term dates (no server-side "current term" filter exists) | 1 | `mitsync sync --dry-run` lists exactly the 7 known courses for the active term |
| FR-3 | Incrementally mirror course files (Files tab + Modules fallback when Files is hidden) into `_canvas/<course>/`, keyed by file `uuid`/`updated_at` | 1 | re-running `sync` with no Canvas changes performs zero downloads |
| FR-4 | Record every synced object in a DuckDB manifest (`state/manifest.duckdb`) | 1 | `doctor` cross-checks manifest rows against `_canvas/` contents with zero drift |
| FR-5 | Pull Canvas pages, announcements, and planner/deadline data | 2 | `mitsync due` lists upcoming items across all 7 courses with correct due dates |
| FR-6 | Propose a filing plan mapping newly synced files into the student's own folders, governed by `config/naming.md` + `config/courses.yml` | 2 | `organize plan` always produces a dry-run diff before any write; contains zero suggested moves of pre-existing files |
| FR-7 | Apply an approved filing plan via hardlink/copy (never move-only from `_canvas/`), writing an undo log | 2 | `organize undo` after an `apply` restores the pre-apply state exactly |
| FR-8 | Read Apple Calendar (EventKit, read-only) for class meeting times | 2 | `mitsync calendar` returns class times sourced from Calendar.app; no EventKit write call exists anywhere in the codebase |
| FR-9 | Extract text from synced documents (PDF/notebook/CSV) | 3 | `_kb/text/` contains one extracted-text file per synced source document |
| FR-10 | Write per-course markdown notes and a knowledge graph (JSONL triples/nodes + DuckDB projection) from extracted content, against a placeholder v0 ontology | 3 | `graph rebuild` reproduces `state/graph.duckdb` byte-identically (modulo timestamps) from `_kb/graph/*.jsonl` alone |
| FR-11 | Every judgment step is reachable via `--driver api` (provider-agnostic), `--driver agent` (task file + exit code 20 + `resolve`), or `--driver rules` (deterministic) | throughout | the same `organize plan` input produces the same destination-folder decisions whether driven by `api` or by an agent resolving the equivalent task |
| FR-12 | No module outside `mitsync/llm/` imports a provider SDK; every command's core takes a `Judge` interface | throughout | `grep` for provider SDK imports outside `mitsync/llm/` returns nothing |
| FR-13 | Generate a "what's due / what to read" briefing per course | 3 | `mitsync brief --course "Analytics Edge"` cites specific due items and specific documents, not just "check Canvas" |
| FR-14 | Optionally run under OpenClaw (cron sync, chat) without any code change required to run standalone | 4 | `mitsync sync` succeeds identically with OpenClaw's gateway stopped |
| FR-15 | Downstream-agent handoff: `_kb/AGENTS.md` alone is sufficient entry point | 3 | see headline acceptance test below |

## Acceptance criteria

- **Headline handoff test:** a fresh agent session given only `_kb/AGENTS.md`
  (no other prompting, no directory exploration hints) can correctly answer
  "what do I need to do for Analytics Edge this week and which documents
  should I read?" by following pointers from that one file, without
  scanning the tree blindly.
- Zero writes to Canvas or Calendar observed across a full `sync` +
  `organize apply` + `calendar` + `kb build` run (verified by a guardrail
  test that fails the build if any write-capable Canvas/EventKit call is
  invoked).
- `organize apply` never modifies a file that existed before `mitsync` was
  first run against that folder, unless the user passed an explicit
  override flag after reviewing a plan naming that file.
- The dual-mode parity test (`docs/ENGINEERING_PLAN.md`) passes: given the
  same inputs, `--driver api` (against a recorded fixture response) and
  `--driver agent` (resolved with the equivalent JSON) produce identical
  on-disk results.
- `mitsync` runs end-to-end with zero environment credentials configured
  (agent-driven mode is the default, not an afterthought).
- `AI_Studio/nandatown` never appears in `_canvas/`, `_kb/`, the manifest,
  or the graph, under any command.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| MIT IS&T may restrict or not document manual Canvas access tokens for students | Blocks FR-1 entirely | Verify empirically at setup time (`docs/RUNBOOK.md` validation step); no code-level mitigation possible, flag early |
| Canvas pre-signed file `url` TTL is undocumented and may expire mid-sync | Corrupt/partial downloads | Never cache the `url`; re-resolve via `GET /files/:id` immediately before each download |
| 403 vs 429 both used for throttling per conflicting docs | Sync could misclassify a permissions error as a rate limit or vice versa | Treat both as backoff-and-retry with a cap, then surface a distinct "still failing after N retries" error rather than looping forever |
| TCC (macOS privacy) grants are bound to the invoking binary/session; a LaunchAgent-run process may lack calendar access even after an interactive grant elsewhere | `calendar` silently returns nothing under OpenClaw's gateway | Require and document the one-time interactive grant under the exact execution context (LaunchAgent) that will run long-term, per `docs/RUNBOOK.md` |
| Ontology is a v0 placeholder; the real one arrives later | Graph structure may need a breaking migration | Keep JSONL canonical and append-only; migrations are a rebuild of the DuckDB projection, not a rewrite of history; document the mismatch-handling strategy in `docs/ENGINEERING_PLAN.md` |
| Node version on the machine (v20.19.2) is below OpenClaw's requirement (24.16+/26.1+) | OpenClaw hosting unavailable until upgraded | OpenClaw is explicitly optional (goal 6); every command must work without it |
| `organize` misfiling a document the student cares about | Erodes trust, possible data loss if compounded with a bad undo | Dry-run-first default, explicit apply step, mandatory undo log, never touch pre-existing files without approval |

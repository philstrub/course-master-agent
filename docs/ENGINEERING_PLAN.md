# Engineering Plan — mitsync

## Architecture

```
                     ┌───────────────────────────────────────────────┐
                     │                  mitsync/cli.py                │
                     │   (typer; parses --driver, dispatches command) │
                     └───────────────────┬───────────────────────────┘
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              │                            │                            │
    ┌─────────▼─────────┐        ┌─────────▼─────────┐        ┌─────────▼─────────┐
    │  I/O-only commands │        │  Judgment commands  │        │  resolve / doctor  │
    │  sync, calendar,   │        │  map, organize plan,│        │                    │
    │  due, extract,     │        │  brief, kb build     │        │                    │
    │  graph rebuild     │        │                      │        │                    │
    └─────────┬─────────┘        └─────────┬─────────┘        └─────────┬─────────┘
              │                            │ calls judge.judge(...)      │ validates +
              │                            │                              applies
              │                  ┌─────────▼─────────┐
              │                  │   Judge interface   │
              │                  │ (mitsync/llm/base.py)│
              │                  └─────────┬─────────┘
              │                            │
              │        ┌───────────────────┼───────────────────┐
              │        │                    │                    │
              │  ┌──────▼──────┐   ┌─────────▼─────────┐  ┌──────▼──────┐
              │  │ api_driver   │   │  agent_driver       │  │ rules_driver │
              │  │ (calls a     │   │  (writes task file, │  │ (deterministic│
              │  │ provider SDK)│   │  raises Pending-     │  │  heuristics) │
              │  └──────────────┘   │  Judgment)           │  └──────────────┘
              │                     └─────────────────────┘
              ▼
    manifest.duckdb, _canvas/, _kb/*, graph.duckdb, state/undo/
```

### Layering rule

The deterministic Python layer (`canvas_client`, `sync`, `organize`'s file
operations, `calendar_read`, `extract`, `graph`'s JSONL/DuckDB I/O) does
every fetch, hash, move, parse, and disk write. It never guesses. The LLM
layer (`mitsync/llm/`) is the *only* place that produces judgment —
classification (which folder), naming (what filename), synthesis (brief
text), and extraction (which triples a paragraph implies). A command's
core function receives a `Judge` and calls `judge.judge(...)` at the exact
point a decision is needed; it never imports a provider SDK, and no module
outside `mitsync/llm/` may do so. This is structurally enforced by a test
that greps for `import anthropic`, `import openai`, `import google.*` etc.
outside `mitsync/llm/` and fails the build if found.

## The `Judge` contract

```python
# mitsync/llm/base.py
class PendingJudgment(Exception):
    def __init__(self, task_file: Path):
        self.task_file = task_file


class Judge(Protocol):
    def judge(self, task: str, payload: dict, schema: dict) -> dict:
        """Return a dict satisfying `schema`. In agent mode, raises
        PendingJudgment instead of returning, carrying the path of the
        task file it just wrote to state/tasks/."""
```

`cli.py` calls the command's core with the constructed `Judge`, and wraps
the call:

```python
try:
    core_fn(..., judge=judge)
except PendingJudgment as pj:
    print(f"Wrote pending task: {pj.task_file}")
    print("Exit code 20 (pending judgment) — resolve this task, then re-run.")
    raise typer.Exit(20)
```

### Driver table

| Driver  | Class              | Behavior on `judge()`                                                                 | Credentials needed |
|---------|---------------------|-----------------------------------------------------------------------------------------|---------------------|
| `api`   | `ApiDriver`         | Calls the configured provider (Anthropic/OpenAI/Google/any OpenAI-compatible base URL) synchronously, validates the response against `schema`, returns it | API key (env or `config/settings.yml`) |
| `agent` | `AgentDriver`       | Writes `state/tasks/<uuid>.json` with `{task, payload, schema, rules, instructions}`, raises `PendingJudgment(task_file)` | none |
| `rules` | `RulesDriver`       | Applies deterministic heuristics from `mitsync/llm/rules_driver.py` (e.g. extension/keyword matching against `config/naming.md`); if no rule matches, returns a `{"unresolved": true}` shaped result rather than guessing | none |

Provider selection for `ApiDriver` reads `config/settings.yml`
(`llm.provider`, `llm.base_url`, `llm.model`) plus environment variables
for the key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, or a
generic `MITSYNC_LLM_API_KEY` for a custom OpenAI-compatible base URL).
Provider choice is never hardcoded in command modules — only
`mitsync/llm/api_driver.py` branches on it.

`--driver` with no explicit value: the CLI checks whether a usable API key
is configured; if yes, defaults to `api`, otherwise defaults to `agent`.
This makes zero-credential operation the true default, not a fallback.

## `resolve`

```
mitsync resolve <task_file> --result <result_file>
```

1. Load `<task_file>`, extract its `schema` and `task` name.
2. Load `<result_file>`, validate against `schema` (jsonschema).
3. On failure: print the validation error, exit non-zero, apply nothing.
4. On success: dispatch to the same deterministic apply logic the
   originating command would have used had `judge()` returned this dict
   directly (e.g. `organize`'s file-move code, `kb build`'s note-writer).
   This is what guarantees dual-mode parity — `resolve` and `ApiDriver`
   converge on the same apply path.
5. Record the task as resolved (move or mark it in `state/tasks/`) so a
   second `resolve` on the same file is a no-op with a clear message.

## Module responsibilities and public signatures

| Module | Responsibility | Key public functions |
|---|---|---|
| `cli.py` | typer app, `--driver` parsing, exit-code-20 handling | `app: typer.Typer` |
| `config.py` | load/merge `config/settings.yml`, `config/courses.yml`, env | `load_settings() -> Settings`; `load_courses() -> dict[str, CourseConfig]` |
| `paths.py` | canonical paths, exclusion rules (`AI_Studio/nandatown`, `.venv`, etc.) | `is_excluded(path: Path) -> bool`; `canvas_root() -> Path`; `kb_root() -> Path` |
| `errors.py` | error taxonomy (below) | `class MitsyncError`, subclasses per row |
| `manifest.py` | DuckDB manifest CRUD | `open_manifest() -> duckdb.Connection`; `upsert_file(record: FileRecord) -> None`; `get_stale(course_id: str) -> list[FileRecord]` |
| `canvas_client.py` | direct `httpx` client (NOT `canvasapi` — we need raw access to `Link`, `X-Rate-Limit-Remaining` and `X-Request-Cost` headers, and to re-resolve pre-signed URLs per download); pagination, throttling, backoff | `list_courses(term_filter) -> list[Course]`; `list_files(course_id) -> list[CanvasFile]`; `list_modules(course_id) -> list[Module]`; `resolve_file_url(file_id) -> str`; `list_planner_items(start, end) -> list[PlannerItem]` |
| `sync.py` | orchestrates canvas_client + manifest + download; pure I/O | `sync(course_ids: list[str] | None, dry_run: bool) -> SyncReport` |
| `organize.py` | filing plan + apply/undo; judgment only at `plan()` | `plan(new_files: list[FileRecord], judge: Judge) -> Plan`; `apply(plan: Plan) -> ApplyReport`; `undo(apply_id: str) -> None` |
| `calendar_read.py` | EventKit CLI wrapper, read-only | `list_class_events(start, end) -> list[ClassEvent]` (no write function exists in this module, by design) |
| `deadlines.py` | cross-course due list from planner items | `list_due(courses: list[str], within_days: int) -> list[DueItem]` |
| `extract.py` | text extraction from PDFs/notebooks/CSVs | `extract_text(source: Path) -> Path` (writes to `_kb/text/`, returns its path) |
| `graph.py` | JSONL append + DuckDB projection/rebuild/query | `append_triples(triples: list[Triple]) -> None`; `rebuild() -> None`; `query(cypher_like: str) -> list[Row]` |
| `kb.py` | orchestrates extract/graph/notes into `_kb/`; judgment at note/triple generation | `build(courses: list[str], judge: Judge) -> KbBuildReport` |
| `notify.py` | briefing generation; judgment at synthesis | `brief(course: str, judge: Judge) -> str` |
| `llm/base.py` | `Judge` protocol, `PendingJudgment` | see above |
| `llm/api_driver.py` | provider-agnostic API calls | `class ApiDriver(Judge)` |
| `llm/agent_driver.py` | task-file writer | `class AgentDriver(Judge)` |
| `llm/rules_driver.py` | deterministic heuristics | `class RulesDriver(Judge)` |
| `llm/tasks/` | JSON schemas + prompt/instruction templates per task name | one `.schema.json` + `.md` pair per task, e.g. `organize.plan.classify_file.schema.json` |

## Data model

### DuckDB manifest (`state/manifest.duckdb`, table `files`)

| Column | Type | Notes |
|---|---|---|
| `canvas_file_id` | BIGINT | Canvas's `id`, primary key |
| `uuid` | VARCHAR | Canvas's stable `uuid`, used for change detection |
| `course_id` | VARCHAR | Canvas course id |
| `course_name` | VARCHAR | resolved via `config/courses.yml` |
| `display_name` | VARCHAR | Canvas `display_name` |
| `content_type` | VARCHAR | MIME type |
| `size` | BIGINT | bytes |
| `updated_at` | TIMESTAMP | Canvas's `updated_at`; drives incrementality |
| `local_path` | VARCHAR | path under `_canvas/<course>/` |
| `synced_at` | TIMESTAMP | when this row was last written |
| `sha256` | VARCHAR | local file hash, for `doctor` drift checks |
| `source` | VARCHAR | `files` \| `modules` \| `pages` — which Canvas surface it came from |

### Graph JSONL record shape (`_kb/graph/nodes.jsonl`, `_kb/graph/triples.jsonl`)

Nodes (one JSON object per line, append-only; a later line for the same
`id` is a correction, last-write-wins on projection):

```json
{"id": "course:analytics-edge", "type": "Course", "props": {"name": "Analytics Edge"}, "ts": "2026-09-20T12:00:00Z", "source": "config/courses.yml"}
```

Triples (append-only, never edited in place — a retraction is a new line
with `"retracted": true` referencing the original by content hash):

```json
{"subject": "doc:analytics-edge/unit3.pdf", "predicate": "coversTopic", "object": "topic:linear-regression", "ontology_version": "v0", "confidence": 0.8, "ts": "2026-09-20T12:00:00Z", "source_task": "kb.build.extract_triples"}
```

### DuckDB graph projection (`state/graph.duckdb`)

- `nodes(id VARCHAR PRIMARY KEY, type VARCHAR, props JSON, updated_at TIMESTAMP)`
- `edges(subject VARCHAR, predicate VARCHAR, object VARCHAR, ontology_version VARCHAR, confidence DOUBLE, ts TIMESTAMP, retracted BOOLEAN)`

`graph rebuild` truncates and replays `_kb/graph/*.jsonl` in full — the
DuckDB file is a cache, never a second source of truth. This is what makes
an ontology migration safe: replay with a new ontology mapping without
touching history.

## Error taxonomy

| Error | Where it surfaces | Handling strategy |
|---|---|---|
| `RateLimitedError` | `canvas_client` on 429 (or 403 that looks like throttling, since docs conflict) | exponential backoff with jitter, capped retries, then raise with the endpoint and elapsed time; never silently drop the sync |
| `TccDeniedError` | `calendar_read` when EventKit reports no/denied access | fail fast with the exact remediation (`docs/RUNBOOK.md`'s interactive-grant step); never fall back to a stale cache or guess class times |
| `FilesTabHiddenError` | `canvas_client.list_files` on 403 | caught internally, not surfaced as a hard failure — `sync` automatically falls back to the Modules API for that course and logs which path was used |
| `CanvasFeatureDisabled` | `canvas_client._request` on a **404** whose body says the feature is disabled (e.g. Pages off: `{"message":"That page has been disabled for this course"}`) | the 404-shaped sibling of a hidden Files tab — expected, never an error: `sync` skips that stage for that course and records it in `SyncReport.notices` (rendered as a dim "skipped" section), so the `errors` count stays meaningful |
| `CanvasNotFound` | `canvas_client._request` on a 404 that does *not* look like a disabled feature | a genuine miss (deleted file, bad id, wrong path) — reported as a real error; never silently swallowed, and never confused with the disabled-feature case |
| `CanvasHTTPError` | `canvas_client._request` / `_stream_to` for any other non-2xx, including a retryable 5xx once retries are exhausted | catch-all so **no raw `httpx.HTTPStatusError` escapes the client** — callers only ever handle `MitsyncError`, which is what makes this taxonomy enforced rather than aspirational |
| `StaleFileUrlError` | `canvas_client.resolve_file_url` when a download 403/404s using a cached `url` | never cache the `url` past a single download attempt; on failure, re-resolve via `GET /files/:id` once and retry, then give up loudly |
| `PendingJudgment` | any judgment call under `--driver agent` | not really an error — caught by `cli.py`, turned into exit code 20 and a printed task path |
| `OntologyMismatchError` | `graph.py` when a triple's `ontology_version` predicate/type isn't in the currently loaded ontology | the triple is still appended to JSONL (never lose data), but excluded from the DuckDB projection and reported by `doctor`; resolved by a future ontology migration, not by mutating history |
| `ManifestCorruptError` / `GraphCorruptError` | `doctor` | both stores are rebuildable-from-source (`manifest.duckdb` from a fresh `sync --full`, `graph.duckdb` from `graph rebuild`); recovery is "delete the DuckDB file and rebuild," never manual SQL surgery |

## Phase breakdown

| Phase | Scope | Depends on |
|---|---|---|
| 1 | Canvas auth + course listing + file sync + manifest (FR-1..4) | none |
| 2 | Deadlines, organize plan/apply/undo, calendar read (FR-5..8) | Phase 1 (organize needs synced files; deadlines needs course listing) |
| 3 | Extract, graph (v0 ontology), kb build, brief, handoff test (FR-9..10, 13, 15) | Phase 1 (content to extract), Phase 2 (deadlines feed `brief`) |
| cross-cutting | Dual execution model, `Judge`, drivers, `resolve` (FR-11, 12) | must exist before any judgment command in Phase 2/3 lands — built alongside Phase 1's `map`/first judgment use, not deferred |
| 4 | OpenClaw hosting (cron, chat) (FR-14) | Phase 1-3 complete and usable standalone first |

## Test plan

- **Unit tests**, stub `Judge`: every judgment command tested against a
  `Judge` test double that returns fixed dicts, so `organize.plan`,
  `kb.build`, `notify.brief` logic is verified without ever calling
  `agent`/`api` machinery. Also unit-tests `RulesDriver`'s heuristics in
  isolation.
- **Integration tests against recorded Canvas fixtures**: `canvas_client`
  calls are recorded once (VCR-style cassettes) covering the 403-hidden-
  files path, pagination via `Link` headers, and a 429. `sync` runs against
  these fixtures with no live network access, asserting manifest state and
  `_canvas/` contents.
- **End-to-end test**: a temp workspace tree seeded with fixture course
  folders + `AI_Studio/nandatown` stand-in (a few thousand dummy files) is
  run through `sync` → `organize plan` → `organize apply` → `kb build`,
  asserting (a) `nandatown` is never touched, (b) pre-existing files are
  never moved, (c) an `organize undo` restores byte-identical state.
- **Dual-mode parity test**: for a fixed `organize plan` payload, run once
  with `ApiDriver` against a recorded fixture model response, and once with
  `AgentDriver` resolved via `resolve` using the JSON-equivalent of that
  same fixture response; assert the resulting file-system diff and
  manifest rows are identical between the two runs.
- **Guardrail test**: grep-based check that no module outside
  `mitsync/llm/` imports a provider SDK, and that `calendar_read.py`
  contains no EventKit write call.

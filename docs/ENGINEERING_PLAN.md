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
    │  due, extract,     │        │  graph extract,      │        │                    │
    │  graph rebuild     │        │  kb build            │        │                    │
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

The deterministic Python layer (`canvas/`, `filing/`'s file operations,
`schedule/calendar`, `knowledge/extract`, `knowledge/graph`'s JSONL/DuckDB I/O) does
every fetch, hash, move, parse, and disk write. It never guesses. The LLM
layer (`mitsync/llm/`) is the *only* place that produces judgment —
classification (which folder), naming (what filename), per-course note
writing (`kb build`), and extraction (which triples a paragraph implies).
A command's core function receives a `Judge` and calls `judge.judge(...)` at the exact
point a decision is needed; it never imports a provider SDK, and no module
outside `mitsync/llm/` may do so. This is structurally enforced by a test
that greps for `import anthropic`, `import openai`, `import google.*` etc.
outside `mitsync/llm/` and fails the build if found.

## Judgment, drivers, and `resolve`

`CLAUDE.md` § "The dual execution model, and how you fit in" is the
authoritative description, with real captured output from an actual run.
Read it there rather than a paraphrase here.

The interface itself is `mitsync/llm/base.py`:

```python
class Judge(Protocol):
    def judge(self, task: JudgeTask) -> dict[str, Any]: ...
```

`JudgeTask` carries `name`, `version`, `schema`, `payload`, `rules`,
`instructions`, `origin_command`, and `origin_args`. The agent driver raises
`PendingJudgment` (from `mitsync/core/errors.py`, not from `llm/base.py`), which
`cli.py` turns into exit code `EXIT_PENDING_JUDGMENT` (20).

Exactly four commands take `--driver` / `--resolve`: `map`, `organize plan`,
`graph extract`, `kb build`. They are the four keys of `REPLAY` in
`mitsync/cli.py`, which is what `resolve` dispatches on.

## Modules, data model, error taxonomy

Deliberately not restated here — a second copy drifts from the code and then
misleads. Read instead:

- `mitsync/core/errors.py` — the whole error hierarchy, one docstring per class.
- The `__init__.py` docstring of each subpackage (`core`, `canvas`, `filing`,
  `schedule`, `knowledge`, `llm`) — the architectural direction and the
  constraints its modules must keep — then the module docstring inside it.
- The `CREATE TABLE` statements in `mitsync/canvas/manifest.py` and
  `mitsync/knowledge/graph.py`
  — the manifest and graph-projection schemas.
- `docs/ADR/0002-graph-storage-duckdb.md` — why the JSONL is canonical and
  `state/graph.duckdb` is a disposable projection.

Tests live in `tests/` and are the executable version of the contracts above.

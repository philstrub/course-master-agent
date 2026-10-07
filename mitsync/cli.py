"""
# CLI

The `mitsync` command line: the only place an error becomes an exit code.

## 1. What This Module Does

Defines the Typer app and wires each command to the sibling module that does
the work. Business logic lives in those modules; this file contains argument
parsing, output (a compact table, or JSON on stdout with `--json`), and
`doctor`.

## 2. Why This Module Exists

It is one of the two exception boundaries the codebase allows. `run()` turns
any escaped `MitsyncError` into a one-line message and exit 1 instead of a
stack trace. Everything below raises; only here does a failure become a
process outcome.

## 3. How It Fits in the Architecture

The top layer, importing everything and imported by nothing. Because the
library is also used directly -- by tests and by OpenClaw skills that never
touch Typer -- no module below may exit the process or print a fatal error, and
this file is where that discipline is paid for.

## 4. Key Concepts

**Data tools only.** Every command reads, writes or validates,
deterministically; none asks for judgment. The driving agent reads `due`,
`work`, `calendar` and `unfiled` output (all `--json`-capable), decides, and
hands its decisions back as data: a plan file for `organize apply --plan`, a
JSONL of graph facts for `graph add`, and the `COURSE.md` files it writes itself.

**`--json` owns stdout.** With `--json` the only thing on stdout is one JSON
document; notices and logs go to stderr, so an agent can pipe the output
straight into a parser.

**Validating and applying are one command, confirming is not optional.**
`organize apply` validates every placement in the agent's plan, reports each
rejection with its reason, and applies the rest only after confirmation
(interactive, or `--yes`). It records an undo log; `organize undo` reverses it.

**`doctor` is the cold-start surface.** It reports what is configured, what is
missing, where each secret came from (never its value), and whether the
workspace directories are writable.

**Why exceptions are caught here.** This is the boundary, so the handlers are
the point rather than an exception to the rule. `run()` catches `MitsyncError`
as the backstop; individual commands catch only the failure they specifically
expect -- a denied calendar grant, or `unfiled --ids` answering
`{"busy": true}` on `ManifestBusy` so the scheduler can tell a running sync
from a broken check -- so that a new failure mode can never be
silently absorbed by a command and still reaches `run()`. `doctor`
additionally catches `ImportError` and `MitsyncError` per check, because
reporting a broken component is its entire job.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from mitsync.canvas import sync as sync_mod
from mitsync.core.config import Settings, load_settings
from mitsync.core.env import DISABLE_ENV, ENVIRONMENT, NOT_SET, dotenv_disabled, load_dotenv
from mitsync.core.errors import ManifestBusy, MitsyncError
from mitsync.core.logging import setup_logging
from mitsync.filing import organize as organize_mod
from mitsync.filing.course_map import naming_rules_path
from mitsync.knowledge import extract as extract_mod
from mitsync.knowledge import graph as graph_mod
from mitsync.knowledge import kb as kb_mod
from mitsync.schedule import calendar as calendar_read
from mitsync.schedule import deadlines, email_brief

console = Console()

app = typer.Typer(
    name="mitsync",
    help=(
        "Data tools for MIT coursework: mirror Canvas, report deadlines and work, "
        "apply agent-written filing plans, and maintain a knowledge base."
    ),
    no_args_is_help=True,
    add_completion=False,
)
organize_app = typer.Typer(help="Apply and undo agent-written filing plans.")
graph_app = typer.Typer(help="Add to, rebuild and query the knowledge graph.")
kb_app = typer.Typer(help="Build the deterministic parts of the markdown knowledge base.")
gradescope_app = typer.Typer(help="Read (never write) the student's Gradescope dashboard.")
forum_app = typer.Typer(help="The Homework 3 agent forum: read it, post to it within bounds.")
forum_knowledge_app = typer.Typer(help="Lecture material the forum agent may draw on.")
app.add_typer(organize_app, name="organize")
app.add_typer(graph_app, name="graph")
app.add_typer(kb_app, name="kb")
app.add_typer(gradescope_app, name="gradescope")
app.add_typer(forum_app, name="forum")
forum_app.add_typer(forum_knowledge_app, name="knowledge")

#: How long `unfiled --ids` waits for a sync's lock before answering busy.
IDS_WAIT_SECONDS = 10.0

JsonOpt = Annotated[bool, typer.Option("--json", help="Print one JSON document to stdout.")]


def _settings() -> Settings:
    s = load_settings()
    s.paths.ensure()
    return s


def _emit_json(doc: Any) -> None:
    """The only thing a `--json` command writes to stdout."""
    sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n")


def _notes(warnings: list[str]) -> None:
    err = Console(stderr=True)
    for warning in warnings:
        err.print(f"[yellow]note:[/yellow] {warning}")


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    setup_logging(verbose)


# --------------------------------------------------------------------------
# sync / extract
# --------------------------------------------------------------------------
@app.command()
def sync(
    course: Annotated[str | None, typer.Option("--course", help="Limit to one course.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change.")] = False,
    full: Annotated[
        bool, typer.Option("--full", help="Ignore the manifest; re-check everything.")
    ] = False,
    graph: Annotated[
        bool, typer.Option("--graph/--no-graph", help="Refresh the knowledge graph afterwards.")
    ] = True,
) -> None:
    """Mirror Canvas files into `_canvas/` (read-only on Canvas), then refresh the graph."""
    settings = _settings()
    report = sync_mod.run_sync(settings, course=course, dry_run=dry_run, full=full)
    if graph and not dry_run:
        _refresh_graph(settings)
    # A sync that recorded errors must not look like success. scripts/mitsync-cron.sh
    # branches on the exit code, so exiting 0 here would report a revoked token as
    # "Completed with no errors" on every scheduled run.
    if report.errors:
        raise SystemExit(1)


def _refresh_graph(settings: Settings) -> None:
    """The graph's deterministic half, run after every sync so it never lags the disk.

    New text first, then the backbone (which re-projects DuckDB), then Neo4j when
    `NEO4J_URI` is set. What is left needs judgment: the closing `graph check`
    line is what `openclaw/triggers/graph-pending.js` wakes the agent for.
    """
    extract_mod.extract_all(settings)  # prints its own summary
    print(f"graph backbone: {graph_mod.build_backbone(settings).summary()}")
    if os.environ.get("NEO4J_URI"):
        from mitsync.knowledge import neo4j_store

        counts = neo4j_store.push(settings)
        print(f"graph push: {counts['nodes']} nodes, {counts['edges']} edges -> neo4j")
    violations = graph_mod.check(settings)
    print("graph check: " + ", ".join(
        f"{sum(v.severity == sev for v in violations)} {sev}" for sev in ("error", "human", "info")
    ))  # fmt: skip


@app.command()
def extract(
    force: Annotated[bool, typer.Option("--force", help="Re-extract unchanged files.")] = False,
) -> None:
    """Extract text from mirrored and filed documents into `_kb/text/`."""
    settings = _settings()
    extract_mod.extract_all(settings, force=force)


# --------------------------------------------------------------------------
# deadlines, work, calendar
# --------------------------------------------------------------------------
@app.command()
def due(
    days: Annotated[
        int, typer.Option("--days", help="Show items due from 12h ago to N days ahead.")
    ] = deadlines.DEFAULT_WINDOW_DAYS,
    as_json: JsonOpt = False,
) -> None:
    """Upcoming deadlines with the student's own submission status; writes `state/due.json`.

    `state/due.json` always holds every known item; `--days` only narrows what
    is printed.
    """
    settings = _settings()
    report = deadlines.build_due(settings)
    now = datetime.now(UTC)
    doc = report.as_dict() | {
        "window_days": days,
        "due_json": str(report.path),
        "items": [i for i in report.items if deadlines.in_window(i, now, days)],
    }
    if as_json:
        _emit_json(doc)
        return
    graph_mod.print_rows(
        f"due in the next {days} days",
        [
            {
                "due": i["due_at"],
                "course": i["course"],
                "title": i["title"],
                "type": i["type"],
                "status": i["status"] or ("submitted" if i["submitted"] else ""),
            }
            for i in doc["items"]
        ],
    )
    _notes(report.warnings)
    console.print(f"[dim]wrote {report.path}[/dim]")


@app.command()
def work(
    course: Annotated[
        str | None, typer.Option("--course", help="One course folder; default is every course.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Files on disk per course, tagged canvas_copy, edited or yours."""
    settings = _settings()
    doc = deadlines.work_report(settings, course)
    if as_json:
        _emit_json(doc)
        return
    rows = [
        {"course": c["course"], "file": f["path"], "tag": f["tag"], "modified": f["modified"]}
        for c in doc["courses"]
        for folder in c["folders"]
        for f in folder["files"]
    ]
    graph_mod.print_rows("work on disk", rows)


@app.command()
def email(
    date: Annotated[
        str | None, typer.Option("--date", help="YYYY-MM-DD; default is today.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and render only; send nothing.")
    ] = False,
    resend: Annotated[
        bool, typer.Option("--resend", help="Send again even if today's brief went out.")
    ] = False,
) -> None:
    """Render the agent's `_kb/briefings/<date>-morning.json` and email it (once per day)."""
    settings = _settings()
    result = email_brief.send_brief(settings, date, dry_run=dry_run, resend=resend)
    console.print(f"rendered {result['html']}")
    if result["sent"]:
        console.print(f"[green]sent[/green] to {result['to']}")
    else:
        console.print("[dim]dry run: nothing sent[/dim]")


@app.command("calendar")
def calendar_cmd(
    days: Annotated[int | None, typer.Option("--days", help="Lookahead window.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Read Apple Calendar events (read-only)."""
    settings = _settings()
    start = datetime.now(UTC)
    end = start + timedelta(days=days or settings.calendar.lookahead_days)
    try:
        events = calendar_read.read_events(settings, start, end)
    except MitsyncError as exc:
        # A missing binary or a denied TCC grant is expected, not exceptional:
        # print the remediation the module supplied rather than a traceback.
        Console(stderr=True).print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if as_json:
        _emit_json([e.to_dict() for e in events])
    else:
        graph_mod.print_rows(
            "Events",
            [{"when": e.when, "title": e.title, "calendar": e.calendar} for e in events],
        )


# --------------------------------------------------------------------------
# filing
# --------------------------------------------------------------------------
@app.command()
def unfiled(
    as_json: JsonOpt = False,
    ids: Annotated[
        bool,
        typer.Option(
            "--ids",
            help=(
                'Only {"file_ids": [...]}: files in mapped courses that no plan left out, '
                'a cheap "anything to do?". {"busy": true} while a sync holds the manifest.'
            ),
        ),
    ] = False,
) -> None:
    """Mirrored files not filed yet, plus the buckets and rules a plan must follow."""
    settings = _settings()
    if ids:
        # The scheduler's gate must answer at once, so it does not wait out a sync.
        try:
            doc = organize_mod.unfiled(settings, wait=IDS_WAIT_SECONDS)
        except ManifestBusy:
            _emit_json({"busy": True})
            return
        _emit_json(
            {
                "file_ids": sorted(
                    f["file_id"] for f in doc["files"] if f["course"] and not f["left_out_reason"]
                )
            }
        )
        return
    doc = organize_mod.unfiled(settings)
    if as_json:
        _emit_json(doc)
        return
    graph_mod.print_rows(
        f"unfiled ({len(doc['files'])})",
        [
            {
                "file_id": f["file_id"],
                "course": f["course"] or "(unmapped)",
                "name": f["display_name"],
                "module": f["module_name"] or "",
            }
            for f in doc["files"]
        ],
    )
    console.print(f"[dim]filing rules: {doc['naming_rules']}[/dim]")


@organize_app.command("apply")
def organize_apply(
    plan_path: Annotated[
        Path,
        typer.Option(
            "--plan",
            help='Agent-written plan: {"placements": [{"file_id", "destination", "reason"}]}.',
        ),
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
    include_existing: Annotated[
        bool,
        typer.Option(
            "--include-existing", help="Allow placements that MOVE pre-existing student files."
        ),
    ] = False,
) -> None:
    """Validate a plan, report every rejected placement, and apply the rest (undoable).

    Whatever was filed is in the graph when it returns (`graph refresh`). Exits 1
    if any placement was rejected or failed, or if nothing was confirmed, so a
    caller can never mistake a partial apply for a clean one.
    """
    settings = _settings()
    report = organize_mod.apply_plan(
        settings, plan_path, yes=yes, include_existing=include_existing
    )
    if report.applied:
        _refresh_graph(settings)
    if report.errors or report.rejected or not report.confirmed:
        raise typer.Exit(1)


@organize_app.command("undo")
def organize_undo(
    log_id: Annotated[
        str | None, typer.Argument(help="Undo log id; default is the most recent.")
    ] = None,
) -> None:
    """Reverse a previously applied plan."""
    settings = _settings()
    organize_mod.undo(settings, log_id)


@gradescope_app.command("sync")
def gradescope_sync(as_json: JsonOpt = False) -> None:
    """Snapshot submission status and scores into `state/gradescope.json` (GRADESCOPE_COOKIE).

    `mitsync due` then shows Gradescope's status on the matching Canvas row.
    """
    from mitsync.gradescope import client as gs_client

    settings = _settings()
    snap = gs_client.sync(settings)
    if as_json:
        _emit_json(snap.model_dump(mode="json"))
        return
    graph_mod.print_rows(
        "gradescope",
        [
            {"course": c.folder or f"(unmapped) {c.shortname}", "title": a.title,
             "due": a.due_at, "status": a.status, "score": a.score, "points": a.points}
            for c in snap.courses
            for a in c.assignments
        ],
    )  # fmt: skip
    unmapped = [c.shortname for c in snap.courses if c.folder is None]
    if unmapped:
        _notes([f"not in courses.yml (add gradescope_id to map): {', '.join(unmapped)}"])


# --------------------------------------------------------------------------
# graph / kb
# --------------------------------------------------------------------------
@graph_app.command("add")
def graph_add(
    records: Annotated[Path, typer.Argument(help="JSONL of nodes and edges the agent wrote.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate only; add nothing.")] = False,
) -> None:
    """Validate agent-written nodes/edges against the ontology and append them (all or nothing)."""
    settings = _settings()
    if dry_run:
        nodes, edges = graph_mod.validate_records(settings, records)
        print(f"graph add --dry-run: OK, {len(nodes)} nodes and {len(edges)} edges, nothing added")
        return
    report = graph_mod.add_records(settings, records)
    print(f"graph add: {report.summary()}")


@graph_app.command("refresh")
def graph_refresh() -> None:
    """Extract new text, rebuild the backbone, push to Neo4j: what every `sync` ends with."""
    _refresh_graph(_settings())


@graph_app.command("backbone")
def graph_backbone(as_json: JsonOpt = False) -> None:
    """Regenerate the deterministic part of the graph from the files on disk."""
    settings = _settings()
    report = graph_mod.build_backbone(settings)
    doc = {"files": report.documents, "nodes": report.nodes, "edges": report.edges,
           "changed": report.changed}  # fmt: skip
    if as_json:
        _emit_json(doc)
        return
    print(f"graph backbone: {report.summary()}" + ("" if report.changed else " (unchanged)"))


@graph_app.command("check")
def graph_check(
    as_json: JsonOpt = False,
    exit_zero: Annotated[
        bool, typer.Option("--exit-zero", help="Exit 0 even when something is open.")
    ] = False,
) -> None:
    """Every structural violation; exit 1 while any `error` or `human` one remains.

    This is the sync loop's stopping condition: `error` violations are the
    agent's work list, `human` ones are escalated, `info` never blocks.
    """
    settings = _settings()
    violations = graph_mod.check(settings)
    counts = {sev: sum(v.severity == sev for v in violations) for sev in ("error", "human", "info")}
    if as_json:
        _emit_json({"ok": not (counts["error"] or counts["human"]), "counts": counts,
                    "violations": [v.model_dump() for v in violations]})  # fmt: skip
    else:
        graph_mod.print_rows("graph check", [v.model_dump() for v in violations])
        print("graph check: " + ", ".join(f"{n} {sev}" for sev, n in counts.items()))
    if (counts["error"] or counts["human"]) and not exit_zero:
        raise typer.Exit(1)


@graph_app.command("schema")
def graph_schema() -> None:
    """The ontology as JSON: node and edge types, attrs, id formats, structural rules."""
    from mitsync.knowledge.ontology import describe

    _emit_json(describe())


@graph_app.command("rebuild")
def graph_rebuild() -> None:
    """Re-derive the whole graph from `_kb/graph/*.jsonl`."""
    settings = _settings()
    graph_mod.rebuild(settings)


@graph_app.command("query")
def graph_query(
    sql: Annotated[str | None, typer.Option("--sql", help="Raw SQL against the graph DB.")] = None,
    canned: Annotated[str | None, typer.Option("--canned", help="Named query.")] = None,
    param: Annotated[
        list[str] | None,
        typer.Option("--param", help="NAME=VALUE for the query's $NAME; repeatable."),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Query the knowledge graph (the local DuckDB projection)."""
    settings = _settings()
    pairs = [p.partition("=") for p in param or []]
    bad = [name for name, eq, _ in pairs if not eq]
    if bad:
        raise MitsyncError(f"--param takes NAME=VALUE, not {bad[0]!r}")
    params = {name: value for name, _, value in pairs} or None
    rows = graph_mod.query(settings, sql=sql, canned=canned, params=params, show=not as_json)
    if as_json:
        _emit_json(rows)


@graph_app.command("push")
def graph_push() -> None:
    """Replace the graph in Neo4j (NEO4J_URI / _USERNAME / _PASSWORD) with the live JSONL graph."""
    from mitsync.knowledge import neo4j_store

    settings = _settings()
    counts = neo4j_store.push(settings)
    print(f"graph push: {counts['nodes']} nodes, {counts['edges']} edges -> neo4j")


@graph_app.command("cypher")
def graph_cypher(
    query: Annotated[str, typer.Argument(help="A read-only Cypher query.")],
    as_json: JsonOpt = False,
) -> None:
    """Run a read-only Cypher query against the graph pushed to Neo4j."""
    from mitsync.knowledge import neo4j_store

    _settings()  # loads .env
    rows = neo4j_store.cypher(query)
    if as_json:
        _emit_json(rows)
        return
    graph_mod.print_rows("cypher", rows)


@kb_app.command("build")
def kb_build() -> None:
    """Rebuild the graph backbone and `_kb/AGENTS.md`. Never touches a COURSE.md."""
    settings = _settings()
    kb_mod.build(settings)


@kb_app.command("check")
def kb_check(
    as_json: JsonOpt = False,
    exit_zero: Annotated[
        bool, typer.Option("--exit-zero", help="Exit 0 even when something is pending.")
    ] = False,
) -> None:
    """What each course's COURSE.md and readings.json do not cover yet; exit 1 until clean."""
    settings = _settings()
    doc = kb_mod.check(settings)
    if as_json:
        _emit_json(doc)
    else:
        rows = [
            {"course": c["course"], "state": state, "path": p}
            for c in doc["courses"]
            for state, paths in (
                ("no COURSE.md", [] if c["exists"] else [c["course_file"]]),
                ("pending", [d["path"] for d in c["pending"]]),
                ("gone", c["gone"]),
            )
            for p in paths
        ]
        rows += [{"course": r["course"], "state": r["code"], "path": r["message"]}
                 for r in doc["readings"]]  # fmt: skip
        graph_mod.print_rows("course master files", rows)
    if not doc["ok"] and not exit_zero:
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# forum (Homework 3): every command prints one JSON document
# --------------------------------------------------------------------------
@forum_app.command("pending")
def forum_pending() -> None:
    """Control line and unseen entries: whether the forum agent should be woken."""
    from mitsync.forum import discussion

    _emit_json(discussion.pending(_settings()))


@forum_app.command("read")
def forum_read(
    thread: Annotated[
        int | None, typer.Option("--thread", help="Any entry id: return its whole thread.")
    ] = None,
) -> None:
    """Unseen entries with context, replies to you, your posts, your budget, your diary."""
    from mitsync.forum import discussion

    _emit_json(discussion.read(_settings(), thread=thread))


@forum_app.command("act")
def forum_act(
    decision: Annotated[
        Path, typer.Option("--decision", help="The agent's decision file in _kb/forum/decisions/.")
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and check every bound; post nothing.")
    ] = False,
) -> None:
    """Post the decision (reply / new_thread) within every bound, or record a skip."""
    from mitsync.forum import discussion

    _emit_json(discussion.act(_settings(), decision, dry_run=dry_run))


@forum_app.command("reset")
def forum_reset() -> None:
    """Clear the consecutive-failure stop. A human's command: the forum wrapper refuses it."""
    from mitsync.forum import discussion

    _emit_json(discussion.reset(_settings()))


@forum_app.command("scholar")
def forum_scholar(
    query: Annotated[str, typer.Argument(help="What to search Google Scholar for.")],
    since: Annotated[
        int | None, typer.Option("--since", help="Only papers from this year on.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Results to return (max 10).")] = 8,
) -> None:
    """Recent papers on a topic: title, link, authors/venue/year, snippet, citations."""
    from mitsync.forum import scholar

    _emit_json(scholar.search(query, since=since, limit=min(limit, 10)))


@forum_knowledge_app.command("search")
def forum_knowledge_search(
    term: Annotated[str, typer.Argument(help="A concept or phrase, e.g. 'duality'.")],
) -> None:
    """Concepts matching TERM, where each is taught, and lecture passages that mention it."""
    from mitsync.forum import knowledge

    _emit_json(knowledge.search(_settings(), term))


@forum_knowledge_app.command("outline")
def forum_knowledge_outline(
    course: Annotated[str, typer.Argument(help="A course folder, e.g. 'AI_Studio'.")],
) -> None:
    """A course's lectures and concepts from its master file (no paths, no assignments)."""
    from mitsync.forum import knowledge

    _emit_json({"course": course, "outline": knowledge.outline(_settings(), course)})


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".mitsync-write-probe"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _secret_source(dotenv_sources: dict[str, str], name: str) -> str:
    """Where a secret came from, given `load_dotenv()`'s mapping. Never the value."""
    if not os.environ.get(name):
        return NOT_SET
    return dotenv_sources.get(name, ENVIRONMENT)


@app.command()
def doctor() -> None:
    """Check the environment and print what works, what is missing, and how to fix it."""
    checks: list[tuple[str, str, str]] = []  # (status, name, detail)

    def add(status: str, name: str, detail: str) -> None:
        checks.append((status, name, detail))

    v = sys.version_info
    add(
        "PASS" if v >= (3, 11) else "FAIL",
        "python",
        f"{v.major}.{v.minor}.{v.micro}" if v >= (3, 11) else "mitsync needs Python 3.11+",
    )

    uv_path = shutil.which("uv")
    add(
        "PASS" if uv_path else "WARN",
        "uv",
        uv_path or "not on PATH; install from https://docs.astral.sh/uv/ to use the Makefile",
    )

    settings: Settings | None = None
    try:
        settings = load_settings()
        loc = settings.source_path
        if loc and loc.exists():
            add("PASS", "config/settings.yml", f"loaded {loc}")
        else:
            add("WARN", "config/settings.yml", f"missing at {loc}; using defaults")
    except MitsyncError as exc:
        add("FAIL", "config/settings.yml", str(exc).splitlines()[0])

    if settings is None:
        _render_doctor(checks)
        raise typer.Exit(1)

    dotenv_sources = load_dotenv()
    # Note the escape hatch in the rows below, but only while it is active.
    hint = f" ({DISABLE_ENV}=0: .env loading is disabled)" if dotenv_disabled() else ""

    tok_env = settings.canvas.token_env
    add(
        "PASS" if settings.canvas.token else "WARN",
        "canvas token",
        f"${tok_env} is set (source: {_secret_source(dotenv_sources, tok_env)})"
        if settings.canvas.token
        else f"${tok_env} is not set{hint}; `mitsync sync` "
        f"will fail. Create a token in Canvas > Account > Settings.",
    )

    for label, key, why in (
        ("gradescope cookie", "GRADESCOPE_COOKIE", "`gradescope sync` will fail"),
        ("neo4j uri", "NEO4J_URI", "`graph push` / `graph cypher` will fail"),
    ):
        add(
            "PASS" if os.environ.get(key) else "WARN",
            label,
            f"${key} is set (source: {_secret_source(dotenv_sources, key)})"
            if os.environ.get(key)
            else f"${key} is not set{hint}; {why}. See .env.example.",
        )

    cal = settings.calendar.cli
    cal_path = shutil.which(cal)
    add(
        "PASS" if cal_path else "WARN",
        "calendar cli",
        cal_path
        or f"'{cal}' not on PATH; `mitsync calendar` will fail. Install it or set "
        f"calendar.cli in config/settings.yml.",
    )

    paths = settings.paths
    add(
        "PASS" if paths.workspace.exists() else "FAIL",
        "workspace",
        str(paths.workspace) if paths.workspace.exists() else f"{paths.workspace} does not exist",
    )
    bad = [str(d) for d in paths.writable_dirs() if not _writable(d)]
    add(
        "PASS" if not bad else "FAIL",
        "writable dirs",
        "all writable" if not bad else "cannot write: " + ", ".join(bad),
    )

    try:
        import duckdb

        add("PASS", "duckdb", f"version {duckdb.__version__}")
    except ImportError as exc:
        add("FAIL", "duckdb", f"import failed ({exc}); run `uv sync`")

    from mitsync.knowledge.ontology import EDGE_TYPES, NODE_TYPES

    add("PASS", "ontology", f"{len(NODE_TYPES)} node types, {len(EDGE_TYPES)} edge types")

    rules = naming_rules_path(settings)
    add(
        "PASS" if rules.is_file() else "WARN",
        "naming rules",
        str(rules) if rules.is_file() else f"{rules} is missing; the agent has no filing policy",
    )

    em = settings.email
    node = em.node or shutil.which("node")
    renderer = (paths.repo / "email" / "node_modules" / "tsx").exists()
    missing = [
        label
        for label, ok in (
            ("email.sender/email.to", em.sender and em.to),
            (f"${em.password_env}", os.environ.get(em.password_env)),
            ("node", node),
            ("`npm install` in _agent/email", renderer),
        )
        if not ok
    ]
    add(
        "PASS" if not missing else "WARN",
        "email",
        f"to {em.to} via {em.smtp_host} "
        f"(password source: {_secret_source(dotenv_sources, em.password_env)})"
        if not missing
        else "`mitsync email` will fail; missing: " + ", ".join(missing),
    )

    _render_doctor(checks)
    if any(status == "FAIL" for status, _, _ in checks):
        raise typer.Exit(1)


def _render_doctor(checks: list[tuple[str, str, str]]) -> None:
    style = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
    table = Table(title="mitsync doctor", show_lines=False)
    table.add_column("", width=4)
    table.add_column("check", style="bold")
    table.add_column("detail", overflow="fold")
    for status, name, detail in checks:
        table.add_row(f"[{style[status]}]{status}[/{style[status]}]", name, detail)
    console.print(table)
    fails = sum(1 for s, _, _ in checks if s == "FAIL")
    warns = sum(1 for s, _, _ in checks if s == "WARN")
    summary = f"{len(checks) - fails - warns} pass, {warns} warn, {fails} fail"
    console.print(f"[dim]{summary}[/dim]")


def run() -> None:
    """Entry point: turn any escaped MitsyncError into a clean message, not a traceback.

    Individual commands handle the errors they expect; this is the backstop so a new
    failure mode can never reach the user as a stack trace.
    """
    try:
        app()
    except MitsyncError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":  # pragma: no cover
    run()

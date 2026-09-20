"""The `mitsync` command line.

Business logic lives in sibling modules owned by other phases; this file only
wires commands to them. Those modules are imported LAZILY inside each command
body so the CLI stays importable before they land, and a missing module prints
a clear "not implemented yet" message and exits 2.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from .config import Settings, load_settings
from .errors import EXIT_PENDING_JUDGMENT, MitsyncError, PendingJudgment
from .logging import setup_logging

console = Console()

app = typer.Typer(
    name="mitsync",
    help="Mirror Canvas, file course materials, and maintain a knowledge base.",
    no_args_is_help=True,
    add_completion=False,
)
organize_app = typer.Typer(help="Plan, apply, and undo the filing of course materials.")
graph_app = typer.Typer(help="Build and query the knowledge graph.")
kb_app = typer.Typer(help="Build the markdown knowledge base.")
app.add_typer(organize_app, name="organize")
app.add_typer(graph_app, name="graph")
app.add_typer(kb_app, name="kb")

DriverOpt = Annotated[
    str | None,
    typer.Option("--driver", help="Judgment driver: api, agent, or rules."),
]

_PHASES = {
    "sync": 2,
    "canvas_client": 2,
    "manifest": 2,
    "organize": 3,
    "calendar_read": 4,
    "deadlines": 4,
    "extract": 5,
    "graph": 6,
    "kb": 7,
    "notify": 8,
}


def _settings() -> Settings:
    s = load_settings()
    s.paths.ensure()
    return s


def _not_implemented(module: str, exc: Exception) -> None:
    phase = _PHASES.get(module, "?")
    console.print(f"[yellow]`mitsync {module}` is not implemented yet (phase {phase}).[/yellow]")
    console.print(f"[dim]{type(exc).__name__}: {exc}[/dim]")
    raise typer.Exit(2)


def _pending(exc: PendingJudgment) -> None:
    console.print()
    console.print(f"[bold yellow]Judgment needed:[/bold yellow] {exc.task_name}")
    console.print(f"[bold]Task file:[/bold] {exc.task_path}")
    console.print()
    console.print(exc.instructions)
    raise typer.Exit(EXIT_PENDING_JUDGMENT)


def _judge(settings: Settings, driver: str | None):
    from .llm.base import get_judge

    return get_judge(settings, driver)


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    setup_logging(verbose)


# --------------------------------------------------------------------------
# phase 2: sync
# --------------------------------------------------------------------------
@app.command()
def sync(
    course: Annotated[str | None, typer.Option("--course", help="Limit to one course.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change.")] = False,
    full: Annotated[
        bool, typer.Option("--full", help="Ignore the manifest; re-check everything.")
    ] = False,
) -> None:
    """Mirror Canvas files into the workspace `_canvas/` tree."""
    settings = _settings()
    try:
        from . import sync as sync_mod

        sync_mod.run_sync(settings, course=course, dry_run=dry_run, full=full)
    except (ImportError, AttributeError) as exc:
        _not_implemented("sync", exc)


# --------------------------------------------------------------------------
# phase 3: map / organize
# --------------------------------------------------------------------------
@app.command("map")
def map_courses(
    apply: Annotated[bool, typer.Option("--apply", help="Write config/courses.yml.")] = False,
    driver: DriverOpt = None,
    resolve: Annotated[
        Path | None, typer.Option("--resolve", help="Apply an agent result JSON.")
    ] = None,
) -> None:
    """Match Canvas courses to the workspace course folders."""
    settings = _settings()
    try:
        from . import organize as organize_mod

        judge = _resolved_judge(settings, driver, resolve)
        organize_mod.suggest_course_map(settings, judge, apply=apply)
    except PendingJudgment as exc:
        _pending(exc)
    except (ImportError, AttributeError) as exc:
        _not_implemented("organize", exc)


@organize_app.command("plan")
def organize_plan(
    include_existing: Annotated[
        bool, typer.Option("--include-existing", help="Also file pre-existing files.")
    ] = False,
    driver: DriverOpt = None,
    resolve: Annotated[
        Path | None, typer.Option("--resolve", help="Apply an agent result JSON.")
    ] = None,
) -> None:
    """Propose destinations for newly mirrored files (writes a plan, changes nothing)."""
    settings = _settings()
    try:
        from . import organize as organize_mod

        judge = _resolved_judge(settings, driver, resolve)
        organize_mod.plan(settings, judge, include_existing=include_existing)
    except PendingJudgment as exc:
        _pending(exc)
    except (ImportError, AttributeError) as exc:
        _not_implemented("organize", exc)


@organize_app.command("apply")
def organize_apply(
    plan_path: Annotated[
        Path | None, typer.Option("--plan", help="Plan file; default is the newest.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Apply a plan, recording an undo log."""
    settings = _settings()
    if not yes and not typer.confirm("Apply this plan to the workspace?", default=False):
        raise typer.Exit(1)
    try:
        from . import organize as organize_mod

        organize_mod.apply_plan(settings, plan_path, yes=yes)
    except (ImportError, AttributeError) as exc:
        _not_implemented("organize", exc)


@organize_app.command("undo")
def organize_undo(
    log_id: Annotated[
        str | None, typer.Argument(help="Undo log id; default is the most recent.")
    ] = None,
) -> None:
    """Reverse a previously applied plan."""
    settings = _settings()
    try:
        from . import organize as organize_mod

        organize_mod.undo(settings, log_id)
    except (ImportError, AttributeError) as exc:
        _not_implemented("organize", exc)


# --------------------------------------------------------------------------
# phase 4: calendar / deadlines
# --------------------------------------------------------------------------
@app.command("calendar")
def calendar_cmd(
    days: Annotated[int | None, typer.Option("--days", help="Lookahead window.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit raw JSON.")] = False,
) -> None:
    """Read Apple Calendar events (read-only)."""
    settings = _settings()
    start = datetime.now(UTC)
    end = start + timedelta(days=days or settings.calendar.lookahead_days)
    try:
        from . import calendar_read

        events = calendar_read.read_events(settings, start, end)
    except (ImportError, AttributeError) as exc:
        _not_implemented("calendar_read", exc)
        return
    except MitsyncError as exc:
        # A missing binary or a denied TCC grant is expected, not exceptional:
        # print the remediation the module supplied rather than a traceback.
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if as_json:
        console.print_json(json.dumps([e.to_dict() for e in events], indent=2, default=str))
    else:
        _print_rows("Events", ["when", "title", "calendar"], events)


@app.command()
def due() -> None:
    """Collect upcoming assignment deadlines."""
    settings = _settings()
    try:
        from . import deadlines

        deadlines.build_due(settings)
    except (ImportError, AttributeError) as exc:
        _not_implemented("deadlines", exc)


@app.command()
def brief() -> None:
    """Write a daily briefing combining deadlines and calendar."""
    settings = _settings()
    try:
        from . import deadlines

        deadlines.write_briefing(settings)
    except (ImportError, AttributeError) as exc:
        _not_implemented("deadlines", exc)


# --------------------------------------------------------------------------
# phase 5-7: extract / graph / kb
# --------------------------------------------------------------------------
@app.command()
def extract(
    force: Annotated[bool, typer.Option("--force", help="Re-extract unchanged files.")] = False,
) -> None:
    """Extract text from mirrored documents into `_kb/text/`."""
    settings = _settings()
    try:
        from . import extract as extract_mod

        extract_mod.extract_all(settings, force=force)
    except (ImportError, AttributeError) as exc:
        _not_implemented("extract", exc)


@graph_app.command("rebuild")
def graph_rebuild() -> None:
    """Re-derive the whole graph from `_kb/graph/*.jsonl`."""
    settings = _settings()
    try:
        from . import graph as graph_mod

        graph_mod.rebuild(settings)
    except (ImportError, AttributeError) as exc:
        _not_implemented("graph", exc)


@graph_app.command("extract")
def graph_extract(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only re-judge text extracted after this ISO date."),
    ] = None,
    driver: DriverOpt = None,
    resolve: Annotated[
        Path | None, typer.Option("--resolve", help="Apply an agent result JSON.")
    ] = None,
) -> None:
    """Turn extracted text into graph nodes and edges (deterministic backbone + judgment)."""
    settings = _settings()
    try:
        from . import graph as graph_mod

        judge = _resolved_judge(settings, driver, resolve)
        graph_mod.extract_graph(settings, judge, since=since)
    except PendingJudgment as exc:
        _pending(exc)
    except (ImportError, AttributeError) as exc:
        _not_implemented("graph", exc)


@graph_app.command("query")
def graph_query(
    sql: Annotated[str | None, typer.Option("--sql", help="Raw SQL against the graph DB.")] = None,
    canned: Annotated[str | None, typer.Option("--canned", help="Named query.")] = None,
) -> None:
    """Query the knowledge graph."""
    settings = _settings()
    try:
        from . import graph as graph_mod

        graph_mod.query(settings, sql=sql, canned=canned)
    except (ImportError, AttributeError) as exc:
        _not_implemented("graph", exc)


@kb_app.command("build")
def kb_build(
    driver: DriverOpt = None,
    resolve: Annotated[
        Path | None, typer.Option("--resolve", help="Apply an agent result JSON.")
    ] = None,
) -> None:
    """Rebuild the markdown knowledge base under `_kb/`.

    Without --driver the build is fully deterministic and cannot fail: course
    notes are written as inventory skeletons. Pass --driver to have the notes
    judged, one course per round trip.
    """
    settings = _settings()
    try:
        from . import kb as kb_mod

        judge = _resolved_judge(settings, driver, resolve) if (driver or resolve) else None
        kb_mod.build(settings, judge)
    except PendingJudgment as exc:
        _pending(exc)
    except (ImportError, AttributeError) as exc:
        _not_implemented("kb", exc)


# --------------------------------------------------------------------------
# resolve: hand an agent's judgment back to the command that asked for it
# --------------------------------------------------------------------------
def _replay_map(settings: Settings, result: dict[str, Any], args: dict[str, Any]) -> None:
    from . import organize as organize_mod

    organize_mod.suggest_course_map(
        settings, _PreJudged(result), apply=bool(args.get("apply", False))
    )


def _replay_organize_plan(settings: Settings, result: dict[str, Any], args: dict[str, Any]) -> None:
    from . import organize as organize_mod

    organize_mod.plan(
        settings,
        _PreJudged(result),
        include_existing=bool(args.get("include_existing", False)),
    )


def _replay_graph_extract(settings: Settings, result: dict[str, Any], args: dict[str, Any]) -> None:
    from . import graph as graph_mod

    graph_mod.extract_graph(settings, _PreJudged(result), since=args.get("since"))


class _PerCourseJudged:
    """Return a resolved result for one course; defer every other course again.

    `kb build` judges one course's notes at a time and writes each course before
    moving on, so resolving a task replays the build, keeps the finished courses,
    and stops at the next unresolved one with a fresh task file.
    """

    def __init__(self, result: dict[str, Any], course: str | None, fallback: Any) -> None:
        self._result = result
        self._course = course
        self._fallback = fallback

    def judge(self, task: Any) -> dict[str, Any]:
        args = getattr(task, "origin_args", None) or {}
        if self._course is not None and args.get("course") == self._course:
            return self._result
        return self._fallback.judge(task)


def _replay_kb_build(settings: Settings, result: dict[str, Any], args: dict[str, Any]) -> None:
    from . import kb as kb_mod

    judge = _PerCourseJudged(result, args.get("course"), _judge(settings, "agent"))
    kb_mod.build(settings, judge)


# Originating command name (stored in the task file) -> replay function.
REPLAY: dict[str, Any] = {
    "map": _replay_map,
    "organize plan": _replay_organize_plan,
    "graph extract": _replay_graph_extract,
    "kb build": _replay_kb_build,
}


class _PreJudged:
    """A Judge that returns an already-validated result, for replay."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def judge(self, task: Any) -> dict[str, Any]:  # noqa: ARG002 -- interface
        return self._result


def _resolved_judge(settings: Settings, driver: str | None, resolve: Path | None):
    """Use a stored agent result if --resolve was passed, else a real driver."""
    if resolve is None:
        return _judge(settings, driver)
    return _PreJudged(json.loads(Path(resolve).read_text()))


@app.command()
def resolve(
    task_path: Annotated[Path, typer.Argument(help="The task file written by the agent driver.")],
    result: Annotated[Path, typer.Option("--result", help="Your JSON answer.")],
) -> None:
    """Validate an agent's judgment and replay the command that needed it."""
    settings = _settings()
    from .llm.agent_driver import load_task, resolve_task

    try:
        validated = resolve_task(task_path, result)
    except MitsyncError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    task = load_task(task_path)
    console.print(f"[green]Result validated[/green] for task '{task.name}'.")
    replay = REPLAY.get(task.origin_command or "")
    if replay is None:
        console.print(
            "[yellow]No originating command recorded; the result is stored next to the "
            "task file for the caller to pick up.[/yellow]"
        )
        return
    try:
        replay(settings, validated, task.origin_args)
    except (ImportError, AttributeError) as exc:
        _not_implemented("organize", exc)


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def _print_rows(title: str, columns: list[str], rows: Any) -> None:
    table = Table(title=title)
    for c in columns:
        table.add_column(c)
    for row in rows or []:
        table.add_row(*[str(row.get(c, "")) for c in columns])
    console.print(table)


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".mitsync-write-probe"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _dotenv_hint() -> str:
    """Note the escape hatch in a doctor row, but only while it is active."""
    from .env import DISABLE_ENV, dotenv_disabled

    return f" ({DISABLE_ENV}=0: .env loading is disabled)" if dotenv_disabled() else ""


def _secret_source(name: str) -> str:
    """Where a secret came from. Never includes the value itself."""
    from .env import source_of

    return source_of(name)


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

    tok_env = settings.canvas.token_env
    add(
        "PASS" if settings.canvas.token else "WARN",
        "canvas token",
        f"${tok_env} is set (source: {_secret_source(tok_env)})"
        if settings.canvas.token
        else f"${tok_env} is not set{_dotenv_hint()}; `mitsync sync` "
        f"will fail. Create a token in Canvas > Account > Settings.",
    )

    key_env = settings.llm.api_key_env
    has_key = bool(settings.llm.api_key)
    driver = settings.resolve_driver(None)
    add(
        "PASS" if has_key else "WARN",
        "llm api key",
        f"${key_env} is set (source: {_secret_source(key_env)})"
        if has_key
        else f"${key_env} is not set{_dotenv_hint()} (fine: the agent driver needs no key)",
    )
    add(
        "PASS",
        "llm driver",
        f"settings say '{settings.llm.driver}' -> resolves to '{driver}' "
        f"(provider {settings.llm.provider}, model {settings.llm.model})",
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

    try:
        from .llm.base import load_task_spec

        names = sorted(p.stem for p in (Path(__file__).parent / "llm" / "tasks").glob("*.json"))
        for n in names:
            load_task_spec(n)
        add("PASS", "judge tasks", ", ".join(names) or "none registered")
    except MitsyncError as exc:
        add("FAIL", "judge tasks", str(exc))

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
    except PendingJudgment as exc:  # pragma: no cover - commands normally catch this
        _pending(exc)
    except MitsyncError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":  # pragma: no cover
    run()

"""calendar_read.py: parsing, failure modes, and the read-only invariant."""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from mitsync import calendar_read
from mitsync.config import Settings
from mitsync.errors import CalendarAccessDenied, MitsyncError

# A captured `ical-guy events --from ... --to ... --json` payload.
ICAL_GUY_JSON = {
    "events": [
        {
            "title": "15.095 Machine Learning Lecture",
            "startDate": "2026-09-21T13:00:00Z",
            "endDate": "2026-09-21T14:30:00Z",
            "calendar": "MIT",
            "location": "E51-315",
            "notes": "Trees and boosting",
            "isAllDay": False,
        },
        {
            "title": "Dentist",
            "startDate": "2026-09-22T09:00:00Z",
            "endDate": "2026-09-22T09:30:00Z",
            "calendar": "Personal",
            "isAllDay": False,
        },
        {
            "title": "Analytics Edge project due",
            "startDate": "2026-09-25",
            "endDate": "2026-09-25",
            "calendar": "MIT",
            "isAllDay": True,
        },
    ]
}

# `ekctl` emits a bare list with snake_case keys and a calendar object.
EKCTL_JSON = [
    {
        "summary": "15.C57 Optimization recitation",
        "start_date": "2026-09-23T15:00:00+00:00",
        "end_date": "2026-09-23T16:00:00+00:00",
        "calendar": {"title": "School"},
        "all_day": False,
        "description": "LP duality",
    }
]

COURSES = [
    {"canvas_id": 1, "folder": "Machine Learning", "course_number": "15.095", "aliases": ["ML"]},
    {"canvas_id": 2, "folder": "Analytics Edge", "course_number": "15.072", "aliases": []},
    {"canvas_id": 3, "folder": "Optimization", "course_number": "15.C57", "aliases": []},
]


def write_courses(settings: Settings) -> None:
    import yaml

    path = settings.paths.config_dir / "courses.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"courses": COURSES}, sort_keys=False))


def _shq(text: str) -> str:
    """Single-quote a string for /bin/sh."""
    return "'" + text.replace("'", "'\"'\"'") + "'"


def fake_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "ical-guy",
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> Path:
    """Install a stub EventKit CLI on PATH that records the argv it was given."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    argv_log = bindir / f"{name}.argv"
    script = bindir / name
    # Only shell builtins: tests deliberately run with a stripped PATH.
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {argv_log}\n'
        f"printf '%s' {_shq(stdout)}\n"
        f"printf '%s' {_shq(stderr)} >&2\n"
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return argv_log


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def test_reads_and_normalises_ical_guy_json(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_courses(settings)
    argv_log = fake_cli(tmp_path, monkeypatch, stdout=json.dumps(ICAL_GUY_JSON))

    events = calendar_read.read_events(settings)

    assert [e.title for e in events] == [
        "15.095 Machine Learning Lecture",
        "Dentist",
        "Analytics Edge project due",
    ]
    lecture = events[0]
    assert lecture.calendar == "MIT"
    assert lecture.location == "E51-315"
    assert lecture.notes == "Trees and boosting"
    assert lecture.all_day is False
    assert lecture.course == "Machine Learning"
    assert events[1].course is None
    assert events[2].all_day is True and events[2].course == "Analytics Edge"

    argv = argv_log.read_text().split()
    assert argv[0] == "events" and "--from" in argv and "--to" in argv and "--json" in argv


def test_reads_ekctl_shape(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_courses(settings)
    settings.calendar.cli = "ekctl"
    fake_cli(tmp_path, monkeypatch, name="ekctl", stdout=json.dumps(EKCTL_JSON))

    events = calendar_read.read_events(settings)

    assert len(events) == 1
    assert events[0].title == "15.C57 Optimization recitation"
    assert events[0].calendar == "School"
    assert events[0].notes == "LP duality"
    assert events[0].course == "Optimization"


def test_empty_output_is_no_events(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout="")
    assert calendar_read.read_events(settings) == []


def test_untagged_when_there_is_no_course_map(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout=json.dumps(ICAL_GUY_JSON))
    assert all(e.course is None for e in calendar_read.read_events(settings))


def test_event_is_renderable_by_the_cli_table(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout=json.dumps(ICAL_GUY_JSON))
    event = calendar_read.read_events(settings)[0]
    assert event.get("title") == "15.095 Machine Learning Lecture"
    assert event.get("calendar") == "MIT"
    assert "13:00" in event.get("when")
    assert json.loads(str(event))["title"] == event.title


def test_non_json_output_is_reported(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout="not json at all")
    with pytest.raises(MitsyncError, match="did not emit JSON"):
        calendar_read.read_events(settings)


# --------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------
def test_missing_binary_is_actionable_not_fatal(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    ok, detail = calendar_read.calendar_available(settings)
    assert ok is False
    assert "brew install ical-guy" in detail
    assert calendar_read.read_events(settings) == []


def test_tcc_denial_explains_the_interactive_grant(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(
        tmp_path,
        monkeypatch,
        stderr="Error: access to calendars was denied by the user",
        exit_code=1,
    )
    with pytest.raises(CalendarAccessDenied) as excinfo:
        calendar_read.read_events(settings)
    message = str(excinfo.value)
    assert "INTERACTIVELY" in message
    assert "terminal" in message.lower()
    assert "launchd" in message or "background" in message


def test_silent_non_zero_exit_is_treated_as_denial(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, exit_code=1)
    with pytest.raises(CalendarAccessDenied):
        calendar_read.read_events(settings)


def test_other_failures_are_plain_errors(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stderr="unknown flag --from", exit_code=2)
    with pytest.raises(MitsyncError) as excinfo:
        calendar_read.read_events(settings)
    assert not isinstance(excinfo.value, CalendarAccessDenied)


def test_timeout_is_surfaced_not_hung(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout="{}")

    def slow(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="ical-guy", timeout=1)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(MitsyncError, match="did not answer"):
        calendar_read.read_events(settings)


# --------------------------------------------------------------------------
# the invariant: this module can only read
# --------------------------------------------------------------------------
MUTATING_TOKENS = {
    "create",
    "add",
    "new",
    "save",
    "edit",
    "update",
    "modify",
    "remove",
    "delete",
    "del",
    "rm",
    "write",
    "set",
    "put",
    "post",
    "patch",
    "import",
    "restore",
}


def test_module_contains_no_mutating_subcommand() -> None:
    """No argv-shaped literal in calendar_read.py could ask EventKit to write."""
    source = Path(calendar_read.__file__).read_text()
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        token = node.value.strip().lstrip("-").lower()
        if " " in token or len(token) > 20:
            continue  # prose, not an argv token
        if token in MUTATING_TOKENS:
            offenders.append((node.lineno, node.value))
    assert not offenders, f"mutating-looking argv token in calendar_read.py: {offenders}"


def test_module_exposes_no_write_function() -> None:
    """Nothing defined here is named like a mutation -- because none exists."""
    tree = ast.parse(Path(calendar_read.__file__).read_text())
    defined = [
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]
    offenders = [
        name
        for name in defined
        if MUTATING_TOKENS & {part for part in name.lower().split("_") if part}
    ]
    assert not offenders


def test_every_configured_subcommand_is_the_read_one() -> None:
    for argv in calendar_read._QUERY_ARGV.values():
        assert argv[0] == "events"
        assert all(part.lstrip("-").lower() not in MUTATING_TOKENS for part in argv)

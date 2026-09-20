"""calendar_read.py: parsing, failure modes, and the read-only invariant.

The payloads under ``tests/fixtures/calendar/`` are the real shapes emitted by
``ical-guy 0.13.0`` (``events list --format json``), captured 2026-09-20 and
cross-checked against the installed man page. No test here touches the network,
a real calendar, or TCC: every subprocess is a stub script written to tmp_path
and put on PATH.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mitsync import calendar_read
from mitsync.config import Settings
from mitsync.errors import CalendarAccessDenied, MitsyncError

FIXTURES = Path(__file__).parent / "fixtures" / "calendar"

#: A fixed non-UTC zone, so date-boundary assertions do not depend on the host.
EASTERN = ZoneInfo("America/New_York")


@pytest.fixture
def eastern_tz(monkeypatch: pytest.MonkeyPatch):
    """Pin the process's *local* zone, which `_day` formats against.

    Without this the date assertions below would pass or fail depending on
    where the machine running them happens to be.
    """
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


#: `ical-guy events list --from ... --to ... --format json --group-by none`
ICAL_GUY_JSON = fixture("ical_guy_events_list.json")
#: The same query with `--group-by date`: events nested inside group objects.
ICAL_GUY_GROUPED = fixture("ical_guy_grouped_by_date.json")
#: `ekctl` emits a bare list with snake_case keys and a calendar object.
EKCTL_JSON = fixture("ekctl_events.json")
#: Real stderr when macOS has not granted the terminal calendar access.
TCC_STDERR = fixture("tcc_denied.txt")

COURSES = [
    {"canvas_id": 1, "folder": "Machine Learning", "course_number": "15.095", "aliases": ["ML"]},
    {"canvas_id": 2, "folder": "Analytics Edge", "course_number": "15.072", "aliases": []},
    {"canvas_id": 3, "folder": "Optimization", "course_number": "15.C57", "aliases": []},
    {
        "canvas_id": 4,
        "folder": "From Anaytics to Action",
        "course_number": "15.681",
        "aliases": ["From Analytics to Action"],
    },
]


def write_courses(settings: Settings) -> None:
    import yaml

    path = settings.paths.config_dir / "courses.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"courses": COURSES}, sort_keys=False))


def _shq(text: str) -> str:
    """Single-quote a string for /bin/sh."""
    return "'" + text.replace("'", "'\"'\"'") + "'"


#: The stub EventKit CLI. POSIX sh builtins only -- no external binary is
#: invoked, so it still works under a stripped PATH. `{argv_log}` records the
#: argv; the date `case` mirrors ical-guy 0.13.0's own validation.
_STUB_SH = """#!/bin/sh
prev=
for arg in "$@"; do
  case "$prev" in
    --from|--to|--start|--end)
      case "$arg" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
        today|today+[0-9]*|today-[0-9]*|tomorrow|yesterday) ;;
        *)
          echo "Error: Invalid date format: '$arg'. Use ISO 8601" \\
               "(2024-03-15), 'today', 'tomorrow', 'today+N'/'today-N'." >&2
          exit 1 ;;
      esac ;;
  esac
  prev="$arg"
done
printf "%s\\n" "$@" > {argv_log}
printf '%s' {stdout}
printf '%s' {stderr} >&2
exit {exit_code}
"""


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
    #
    # The stub VALIDATES the dates it is handed, the way ical-guy 0.13.0 does.
    # Without that it accepted any argv at all -- which is exactly how a full
    # ISO 8601 timestamp (rejected live with `Invalid date format`) sailed past
    # a green test suite. Accepted here: YYYY-MM-DD, today, today+N, today-N,
    # tomorrow, yesterday.
    script.write_text(
        _STUB_SH.format(
            argv_log=argv_log,
            stdout=_shq(stdout),
            stderr=_shq(stderr),
            exit_code=exit_code,
        )
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return argv_log


def recorded_argv(argv_log: Path) -> list[str]:
    """The argv the stub CLI saw, one token per line (tokens may contain spaces)."""
    return argv_log.read_text().splitlines()


# --------------------------------------------------------------------------
# the invocation itself -- this is the bug that was fixed
# --------------------------------------------------------------------------
def test_issues_the_verified_ical_guy_invocation(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`events list ... --format json --group-by none`, as verified against 0.13.0.

    The old code ran `events --json`, which ical-guy 0.13.0 rejects with
    `Unknown option '--json'` (exit 64): `events` is a parent sub-command and
    the format is chosen with `--format`.
    """
    argv_log = fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)

    calendar_read.read_events(settings)

    argv = recorded_argv(argv_log)
    assert argv[:2] == ["events", "list"]
    assert "--json" not in argv
    for flag, value in (("--format", "json"), ("--group-by", "none")):
        assert argv[argv.index(flag) + 1] == value
    # Plain YYYY-MM-DD dates, start before end. A full ISO 8601 timestamp is
    # rejected by 0.13.0 with `Invalid date format`.
    start, end = argv[argv.index("--from") + 1], argv[argv.index("--to") + 1]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", start)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", end)
    assert start < end


def test_exact_argv_for_a_seven_day_window(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eastern_tz: None
) -> None:
    """The whole command line, pinned token for token.

    This is the contract with ical-guy 0.13.0. If it changes, that is a
    decision someone made, not a regression that slipped through.
    """
    argv_log = fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)
    start = datetime(2026, 9, 21, 9, 30, tzinfo=EASTERN)
    end = start + timedelta(days=7)

    calendar_read.read_events(settings, start, end)

    assert recorded_argv(argv_log) == [
        "events",
        "list",
        "--from",
        "2026-09-21",
        "--to",
        "2026-09-28",
        "--format",
        "json",
        "--group-by",
        "none",
    ]


def test_dates_are_local_not_utc(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eastern_tz: None
) -> None:
    """20:00 in New York is already tomorrow in UTC -- the window must not shift.

    Formatting the UTC date here would quietly ask for the wrong day every
    evening.
    """
    argv_log = fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)
    evening = datetime(2026, 9, 20, 20, 0, tzinfo=EASTERN)
    assert evening.astimezone(UTC).strftime("%Y-%m-%d") == "2026-09-21"

    calendar_read.read_events(settings, evening, evening + timedelta(days=1))

    argv = recorded_argv(argv_log)
    assert argv[argv.index("--from") + 1] == "2026-09-20"


def test_a_full_iso_timestamp_would_be_rejected(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub enforces ical-guy's date grammar, so the old bug cannot return.

    Guards the guard: if `_day` ever goes back to emitting a timestamp, the
    tests above fail here rather than only in production.
    """
    fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)
    monkeypatch.setattr(calendar_read, "_day", lambda moment: moment.astimezone(UTC).isoformat())
    with pytest.raises(MitsyncError, match="Invalid date format"):
        calendar_read.read_events(settings)


def test_invocation_never_contains_a_write_subcommand(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever else changes, the argv actually executed stays read-only."""
    argv_log = fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)
    calendar_read.read_events(settings)
    for token in recorded_argv(argv_log):
        assert token.lstrip("-").lower() not in MUTATING_TOKENS


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def test_reads_and_normalises_ical_guy_json(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_courses(settings)
    fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)

    events = calendar_read.read_events(settings)

    assert [e.title for e in events] == [
        "15.681 From Analytics to Action (Session 1)",
        "GYM",
        "Analytics Edge - Group formation due",
    ]
    lecture = events[0]
    assert lecture.start == "2026-09-21T12:30:00Z"
    assert lecture.end == "2026-09-21T15:30:00Z"
    # `calendar` is an object in the real payload; we want its title.
    assert lecture.calendar == "Calendar"
    assert lecture.location == "Classroom E62-233"
    assert lecture.notes.startswith("15.681 From Analytics to Action")
    assert lecture.all_day is False
    assert lecture.course == "From Anaytics to Action"


def test_optional_fields_absent_become_none(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GYM event carries no location and no notes -- and no KeyError."""
    write_courses(settings)
    fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)

    gym = next(e for e in calendar_read.read_events(settings) if e.title == "GYM")
    assert gym.location is None
    assert gym.notes is None
    assert gym.calendar == "filippostrub@gmail.com"
    assert gym.course is None


def test_all_day_event(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_courses(settings)
    fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)

    due = calendar_read.read_events(settings)[-1]
    assert due.all_day is True
    assert due.course == "Analytics Edge"
    # An all-day event renders without a clock time.
    assert ":" not in due.when


def test_unknown_keys_are_ignored(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """attendees / recurrence / availability / timeZone never reach Event."""
    fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)
    event = calendar_read.read_events(settings)[0]
    assert set(event.to_dict()) == {
        "start",
        "end",
        "title",
        "calendar",
        "location",
        "notes",
        "all_day",
        "course",
        "when",
    }


def test_grouped_payload_is_flattened(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray `--group-by date` shape must not be mis-read as events."""
    write_courses(settings)
    fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_GROUPED)

    events = calendar_read.read_events(settings)
    assert [e.title for e in events] == ["15.C57 Optimization recitation"]
    assert events[0].course == "Optimization"


def test_object_wrapped_payload_is_unwrapped(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapped = json.dumps({"events": json.loads(ICAL_GUY_JSON)})
    fake_cli(tmp_path, monkeypatch, stdout=wrapped)
    assert len(calendar_read.read_events(settings)) == 3


def test_reads_ekctl_shape(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_courses(settings)
    settings.calendar.cli = "ekctl"
    argv_log = fake_cli(tmp_path, monkeypatch, name="ekctl", stdout=EKCTL_JSON)

    events = calendar_read.read_events(settings)

    assert len(events) == 1
    assert events[0].title == "15.C57 Optimization recitation"
    assert events[0].calendar == "School"
    assert events[0].notes == "LP duality"
    assert events[0].course == "Optimization"
    # ekctl keeps its own adapter: a flat `events` query with --start/--end.
    argv = recorded_argv(argv_log)
    assert argv[0] == "events" and "--start" in argv and "--end" in argv


def test_empty_output_is_no_events(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout="")
    assert calendar_read.read_events(settings) == []


def test_empty_json_array_is_no_events(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout="[]")
    assert calendar_read.read_events(settings) == []


def test_untagged_when_there_is_no_course_map(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)
    assert all(e.course is None for e in calendar_read.read_events(settings))


def test_event_is_renderable_by_the_cli_table(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)
    event = calendar_read.read_events(settings)[0]
    assert event.get("title") == "15.681 From Analytics to Action (Session 1)"
    assert event.get("calendar") == "Calendar"
    assert "12:30" in event.get("when")
    assert json.loads(str(event))["title"] == event.title


def test_non_json_output_is_reported(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout="not json at all")
    with pytest.raises(MitsyncError, match="did not emit JSON"):
        calendar_read.read_events(settings)


def test_truncated_json_is_reported_not_traced(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-written array is a clear error, never a raw JSONDecodeError."""
    fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON[:120])
    with pytest.raises(MitsyncError) as excinfo:
        calendar_read.read_events(settings)
    assert "did not emit JSON" in str(excinfo.value)
    assert not isinstance(excinfo.value, json.JSONDecodeError)


def test_json_scalar_is_reported(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout="42")
    with pytest.raises(MitsyncError, match="neither a list nor an object"):
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


def test_calendar_available_reports_the_resolved_path(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout="[]")
    ok, detail = calendar_read.calendar_available(settings)
    assert ok is True
    assert str(tmp_path / "bin" / "ical-guy") in detail


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


def test_real_ical_guy_tcc_stderr_is_recognised(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verbatim 0.13.0 denial message, not a paraphrase."""
    fake_cli(tmp_path, monkeypatch, stderr=TCC_STDERR, exit_code=1)
    with pytest.raises(CalendarAccessDenied) as excinfo:
        calendar_read.read_events(settings)
    message = str(excinfo.value)
    assert "System Settings" in message
    assert "Privacy & Security > Calendars" in message
    # It quotes the exact command the student should run by hand.
    assert "events list" in message and "--format json" in message


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


def test_usage_error_is_not_mistaken_for_a_denial(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 64 + `Unknown option` is a CLI-drift bug, not a permissions bug."""
    fake_cli(
        tmp_path,
        monkeypatch,
        stderr="Error: Unknown option '--json'.\nUsage: ical-guy events list <options>",
        exit_code=64,
    )
    with pytest.raises(MitsyncError) as excinfo:
        calendar_read.read_events(settings)
    assert not isinstance(excinfo.value, CalendarAccessDenied)
    assert "exit 64" in str(excinfo.value)


def test_timeout_is_surfaced_not_hung(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cli(tmp_path, monkeypatch, stdout="[]")

    def slow(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="ical-guy", timeout=1)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(MitsyncError, match="did not answer"):
        calendar_read.read_events(settings)


def test_timeout_is_bounded(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled EventKit query can never hang the caller forever."""
    seen: dict[str, object] = {}
    real_run = subprocess.run
    fake_cli(tmp_path, monkeypatch, stdout="[]")

    def spy(*args: object, **kwargs: object):
        seen.update(kwargs)
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", spy)
    calendar_read.read_events(settings)
    assert seen["timeout"] == calendar_read.TIMEOUT_SECONDS
    assert 0 < calendar_read.TIMEOUT_SECONDS <= 60


# --------------------------------------------------------------------------
# course tagging
# --------------------------------------------------------------------------
def test_tag_course_matches_aliases(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_courses(settings)
    event = calendar_read.Event(
        start="2026-09-24T18:00:00Z",
        end="2026-09-24T19:00:00Z",
        # The alias is spelled correctly; the folder on disk is not. The
        # alias is what makes the two meet.
        title="From Analytics to Action office hours",
        calendar="MIT",
    )
    assert calendar_read.tag_course(settings, event) == "From Anaytics to Action"


def test_tag_course_matches_the_number_in_a_location_or_notes(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_courses(settings)
    event = calendar_read.Event(
        start="2026-09-24T18:00:00Z",
        end="2026-09-24T19:00:00Z",
        title="Recitation",
        calendar="MIT",
        notes="15_C57 problem set review",
    )
    assert calendar_read.tag_course(settings, event) == "Optimization"


def test_tagging_survives_a_broken_course_map(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = settings.paths.config_dir / "courses.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("courses: [oops: [")
    fake_cli(tmp_path, monkeypatch, stdout=ICAL_GUY_JSON)
    assert all(e.course is None for e in calendar_read.read_events(settings))


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


def test_ical_guy_adapter_is_the_documented_read_pair() -> None:
    """`events list` -- the pair verified against ical-guy 0.13.0."""
    assert calendar_read._QUERY_ARGV["ical-guy"][:2] == ["events", "list"]
    assert calendar_read._DEFAULT_ARGV is calendar_read._QUERY_ARGV["ical-guy"]


def test_unknown_cli_falls_back_to_the_ical_guy_adapter() -> None:
    moment = datetime(2026, 9, 21, tzinfo=UTC)
    argv = calendar_read._argv("/opt/homebrew/bin/some-fork", moment, moment)
    assert argv[1:3] == ["events", "list"]


def test_module_documents_the_verified_version() -> None:
    """The next person hitting a CLI change must find what was assumed."""
    doc = calendar_read.__doc__ or ""
    assert "ical-guy 0.13.0" in doc
    assert "2026-09-20" in doc
    assert "events list --from" in doc

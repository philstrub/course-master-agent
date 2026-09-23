"""The Calendar helper app stays read-only, and the shim fails clearly.

`calendar-helper/main.swift` is the only code in the repo that links EventKit,
so guardrail 2 in CLAUDE.md (never write to Apple Calendar) is enforced here
by scanning it for every EventKit call that creates, changes or deletes.
No test builds or launches the app: that needs a GUI session and a TCC grant.
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SWIFT = (REPO / "calendar-helper" / "main.swift").read_text()
SHIM = REPO / "bin" / "mitsync-calendar"

#: EventKit calls that write, and constructors of things only a write needs.
WRITE_APIS = [
    r"\.save\(",
    r"\.remove\(",
    r"\.commit\(",
    r"\.reset\(",
    r"\.saveCalendar\(",
    r"\.removeCalendar\(",
    r"requestWriteOnlyAccess",
    r"EKEvent\(eventStore",
    r"EKCalendar\(for",
    r"EKReminder",
    r"EKAlarm",
]


def _code(swift: str) -> str:
    """The Swift source without `//` comments, which name the forbidden calls."""
    return "\n".join(line.split("//", 1)[0] for line in swift.splitlines())


def test_helper_calls_no_eventkit_write_api():
    code = _code(SWIFT)
    offenders = [api for api in WRITE_APIS if re.search(api, code)]
    assert not offenders, f"EventKit write API in calendar-helper/main.swift: {offenders}"


def test_helper_reads_events_only():
    code = _code(SWIFT)
    assert "store.events(matching:" in code
    assert "requestAccess(to: .event)" in code


def test_shim_explains_a_missing_app(tmp_path):
    proc = subprocess.run(
        [str(SHIM), "events", "list", "--from", "today"],
        env={"MITSYNC_CALENDAR_APP": str(tmp_path / "Nope.app"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 69
    assert "make calendar-helper" in proc.stderr

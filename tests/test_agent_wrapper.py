"""`bin/mitsync-agent` refuses the commands that change the student's folders.

OpenClaw's exec allowlist holds only this wrapper, so the wrapper's refusal is
the whole guarantee that an autonomous run cannot apply or undo a filing plan.
The refusal happens before uv is looked up, so these tests never run mitsync.
"""

import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "mitsync-agent"
REFUSED = 2


@pytest.mark.parametrize(
    "args",
    [
        ["organize", "apply", "--plan", "p.json"],
        ["organize", "apply", "--plan", "p.json", "--yes"],
        ["organize", "--yes", "apply", "--plan", "p.json"],
        ["organize", "undo"],
        ["organize", "undo", "--last"],
    ],
)
def test_mutating_organize_commands_are_refused(args):
    proc = subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, check=False)
    assert proc.returncode == REFUSED
    assert "human" in proc.stderr

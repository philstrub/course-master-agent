"""`bin/mitsync-agent` refuses what only a human may do to the student's folders.

OpenClaw's exec allowlist holds only this wrapper, so the wrapper's refusal is
the whole guarantee that an autonomous run cannot undo a filing or move a
pre-existing file. Plain `organize apply` (Canvas copies only) is allowed.
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
        ["organize", "apply", "--plan", "p.json", "--include-existing"],
        ["organize", "apply", "--include-existing", "--plan", "p.json", "--yes"],
        ["organize", "--include-existing", "apply", "--plan", "p.json"],
        ["organize", "undo"],
        ["organize", "undo", "--last"],
    ],
)
def test_mutating_organize_commands_are_refused(args):
    proc = subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, check=False)
    assert proc.returncode == REFUSED
    assert "human" in proc.stderr


def test_plain_apply_reaches_mitsync(tmp_path):
    """Not refused: with no uv on the fake HOME it fails later, with a different message."""
    proc = subprocess.run(
        [str(WRAPPER), "organize", "apply", "--plan", "p.json", "--yes"],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != REFUSED
    assert "human" not in proc.stderr

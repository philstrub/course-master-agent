"""The package layering is one-directional, and this is what makes it true.

`mitsync` is split into `core` (the foundation), four capability packages
(`canvas`, `filing`, `schedule`, `knowledge`) and `cli`. The split is
only worth having if the arrows point one way: a foundation module that reaches
back up into a feature package puts an import cycle one careless edit away, and
undoes the readability the split was for.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "mitsync"


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_core_does_not_import_the_layers_above_it() -> None:
    above = {"canvas", "filing", "schedule", "knowledge", "cli"}
    offenders: list[str] = []
    for path in sorted((PKG / "core").rglob("*.py")):
        for name in _imports(path):
            parts = name.split(".")
            if parts[0] == "mitsync" and parts[1:2] and parts[1] in above:
                offenders.append(f"{path.relative_to(PKG.parent)} imports {name}")
    assert not offenders, "mitsync/core reached upward:\n" + "\n".join(offenders)


def test_deadlines_can_be_imported_without_httpx() -> None:
    """Deadlines and work status must be producible with no network and no Canvas token.

    `deadlines` reads the `_meta/*.json` the mirror already wrote rather than
    calling Canvas. Run in a subprocess because the import has to be measured on
    a clean interpreter -- inside the test session everything is already in
    `sys.modules`. Reading the sync manifest is allowed; importing the Canvas
    *client*, and therefore `httpx`, is not.
    """
    probe = (
        "import sys; "
        "import mitsync.schedule.deadlines, mitsync.schedule.calendar; "
        "print(','.join(m for m in ('httpx', 'mitsync.canvas.client', "
        "'mitsync.canvas.sync', 'mitsync.filing.organize') if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"importing deadlines dragged in: {out}"


#: Model SDKs. mitsync is data tools only -- the driving agent brings the model.
FORBIDDEN_SDKS = {"anthropic", "openai", "google.generativeai", "google", "litellm"}


def test_no_model_sdk_is_imported_anywhere() -> None:
    """Structural invariant: mitsync never calls a model, so it imports no SDK."""
    offenders: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        for name in _imports(path):
            if name.split(".")[0] in FORBIDDEN_SDKS or name in FORBIDDEN_SDKS:
                offenders.append(f"{path.relative_to(PKG.parent)} imports {name}")
    assert not offenders, "model SDK imported:\n" + "\n".join(offenders)

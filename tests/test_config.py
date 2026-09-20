"""
# Config Tests

Settings loading, driver resolution, ignore globs, and the workspace boundary.

Covers four things that have to hold before any command is safe to run: a
settings file parses into the expected tree and an invalid one raises
`ConfigError` naming the file; `resolve_driver()` follows the documented
precedence (explicit flag > settings > `auto`, where `auto` means `api` with a
key and `agent` without); `should_ignore()` matches gitignore-style globs,
including with the settings file entirely absent, because the built-in
guardrail globs must survive that; and `Paths` containment rejects paths that
escape the workspace.

Uses the shared `settings` and `workspace` fixtures from `conftest.py`, plus
bare `tmp_path` for the no-settings-file cases, where the point is precisely
that no fixture-supplied config is in play.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mitsync.config import Settings, load_settings
from mitsync.errors import ConfigError
from mitsync.paths import Paths


def test_loads_yaml(settings: Settings) -> None:
    assert settings.canvas.base_url == "https://canvas.mit.edu/api/v1"
    assert settings.llm.api_key_env == "TEST_LLM_KEY"
    assert settings.calendar.lookahead_days == 14


def test_auto_driver_without_key_is_agent(settings: Settings) -> None:
    assert settings.resolve_driver(None) == "agent"


def test_auto_driver_with_key_is_api(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "sk-test")
    assert settings.resolve_driver(None) == "api"


def test_cli_override_wins(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "sk-test")
    assert settings.resolve_driver("rules") == "rules"
    assert settings.resolve_driver("agent") == "agent"


def test_explicit_settings_driver_skips_auto(settings: Settings) -> None:
    settings.llm.driver = "rules"
    assert settings.resolve_driver(None) == "rules"


def test_unknown_driver_raises(settings: Settings) -> None:
    with pytest.raises(ConfigError):
        settings.resolve_driver("telepathy")


@pytest.mark.parametrize(
    "relpath",
    [
        "AI_Studio/nandatown/x/y.py",
        "AI_Studio/nandatown/README.md",
        ".DS_Store",
        "Machine Learning/.DS_Store",
        "Analytics Lab/.venv/lib/python3.11/os.py",
        "x/node_modules/pkg/index.js",
        "a/__pycache__/m.pyc",
        "env/lib/site-packages/numpy/__init__.py",
    ],
)
def test_ignored(settings: Settings, relpath: str) -> None:
    assert settings.should_ignore(relpath) is True


@pytest.mark.parametrize(
    "relpath",
    [
        "Machine Learning/Lec1.pdf",
        "AI_Studio/nandatown.md",
        "AI_Studio/deliverable/report.pdf",
        "Optimization/assignments/pset3.ipynb",
    ],
)
def test_not_ignored(settings: Settings, relpath: str) -> None:
    assert settings.should_ignore(relpath) is False


def test_should_ignore_accepts_path_objects(settings: Settings) -> None:
    assert settings.should_ignore(Path("AI_Studio/nandatown/x/y.py")) is True


def test_missing_settings_file_uses_defaults(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "nope.yml")
    assert s.canvas.token_env == "CANVAS_TOKEN"
    assert s.llm.driver == "auto"


@pytest.mark.parametrize(
    "relpath",
    [
        "AI_Studio/nandatown/x/y.py",
        "AI_Studio/nandatown/README.md",
        "nandatown/notes.md",
        "Analytics Lab/.venv/lib/python3.11/os.py",
    ],
)
def test_guardrails_survive_a_missing_settings_file(tmp_path: Path, relpath: str) -> None:
    """Guardrail 5 must not depend on config/settings.yml existing.

    `load_settings` falls back to `{}` when the file is absent, so the
    nandatown exclusion has to live in DEFAULT_IGNORE_GLOBS or a fresh
    checkout would silently walk it.
    """
    s = load_settings(tmp_path / "definitely-absent.yml")
    assert s.source_path is not None and not s.source_path.exists()
    assert s.should_ignore(relpath) is True


def test_invalid_settings_raise(tmp_path: Path) -> None:
    bad = tmp_path / "settings.yml"
    bad.write_text("canvas:\n  unknown_key: 1\n")
    with pytest.raises(ConfigError):
        load_settings(bad)


def test_path_containment(workspace: Path) -> None:
    paths = Paths(workspace=workspace, repo=workspace / "_agent")
    assert paths.is_inside_workspace(workspace / "Machine Learning" / "a.pdf")
    assert paths.is_inside_workspace("Machine Learning/a.pdf")
    assert paths.is_inside_workspace(workspace)
    assert not paths.is_inside_workspace("/etc/passwd")
    assert not paths.is_inside_workspace(workspace / ".." / "elsewhere")
    assert paths.safe_relative(workspace / "Machine Learning" / "a.pdf") == Path(
        "Machine Learning/a.pdf"
    )
    with pytest.raises(ValueError):
        paths.safe_relative("/etc/passwd")


def test_paths_layout(workspace: Path) -> None:
    paths = Paths(workspace=workspace, repo=workspace / "_agent").ensure()
    assert paths.canvas_mirror == workspace / "_canvas"
    assert paths.kb_text == workspace / "_kb" / "text"
    assert paths.manifest_db == workspace / "_agent" / "state" / "manifest.duckdb"
    assert paths.graph_db == workspace / "_agent" / "state" / "graph.duckdb"
    assert paths.tasks_dir.is_dir()
    assert paths.undo_dir.is_dir()

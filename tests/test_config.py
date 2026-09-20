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

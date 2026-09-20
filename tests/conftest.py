from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mitsync import config as config_mod
from mitsync.config import Settings, load_settings
from mitsync.paths import Paths

SETTINGS_YML = textwrap.dedent(
    """
    canvas:
      base_url: https://canvas.mit.edu/api/v1
      token_env: CANVAS_TOKEN
    llm:
      driver: auto
      provider: anthropic
      model: claude-opus-5
      api_key_env: TEST_LLM_KEY
    calendar:
      cli: ical-guy
      lookahead_days: 14
    ignore_globs:
      - "**/.DS_Store"
      - "**/.venv/**"
      - "**/site-packages/**"
      - "**/node_modules/**"
      - "**/__pycache__/**"
      - "AI_Studio/nandatown/**"
    """
)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    config_mod.clear_cache()
    yield
    config_mod.clear_cache()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp workspace containing an `_agent` repo with a config dir."""
    ws = tmp_path / "courses"
    (ws / "_agent" / "config").mkdir(parents=True)
    (ws / "Machine Learning").mkdir()
    (ws / "AI_Studio").mkdir()
    (ws / "_agent" / "config" / "settings.yml").write_text(SETTINGS_YML)
    monkeypatch.setenv("MITSYNC_WORKSPACE", str(ws))
    monkeypatch.delenv("TEST_LLM_KEY", raising=False)
    return ws


@pytest.fixture
def settings(workspace: Path) -> Settings:
    s = load_settings(workspace / "_agent" / "config" / "settings.yml")
    # Point paths at the temp repo rather than the real one.
    s._paths = Paths(workspace=workspace, repo=workspace / "_agent")
    s.paths.ensure()
    return s

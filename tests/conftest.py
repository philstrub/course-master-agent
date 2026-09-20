"""
# Test Fixtures

The temp workspace every test runs against, so no test can see the real one.

## 1. What This Module Does

Provides three fixtures. `workspace` builds a throwaway course directory under
`tmp_path` containing an `_agent/config/settings.yml` and a couple of course
folders, and points `MITSYNC_WORKSPACE` at it. `settings` loads that file and
re-points `Settings.paths` at the temp repo. `_clear_settings_cache` is
autouse and drops the `lru_cache` in `mitsync.core.config` around every test.

## 2. Why This Module Exists

`mitsync` operates on the user's actual course folders and can move files, so
a test that resolved the real workspace would be destructive. Redirecting the
root in one place makes that impossible by construction rather than by every
test remembering to.

The autouse cache clear exists because `load_settings` is cached per path:
without it, one test's monkeypatched environment would leak into the next
through a cached `Settings` object.

## 3. The Fixture Contract

Anything taking `settings` gets a workspace at `<tmp>/courses` with
`Machine Learning/` and `AI_Studio/` already present, every mitsync directory
created by `Paths.ensure()`, and `TEST_LLM_KEY` -- the API-key variable named
by the test settings -- explicitly unset, so `resolve_driver()` answers
`agent` unless a test sets it. `AI_Studio/nandatown/**` is in the test ignore
globs because several tests assert that the exclusion actually prunes.

No fixture here touches the network, a real Canvas token, a real calendar, or
a provider SDK.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mitsync.core import config as config_mod
from mitsync.core.config import Settings, load_settings
from mitsync.core.paths import Paths

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

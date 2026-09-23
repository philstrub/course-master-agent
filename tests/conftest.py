"""
# Test Fixtures

The temp workspace every test runs against, so no test can see the real one.

## 1. What This Module Does

Provides three fixtures. `workspace` builds a throwaway course directory under
`tmp_path` containing an `_agent/config/settings.yml` and a couple of course
folders (mapped in `_agent/config/courses.yml`), and points
`MITSYNC_WORKSPACE` at it. `settings` loads that file and
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
`Machine Learning/` and `AI_Studio/` already present and mapped in
`courses.yml`, and every mitsync directory created by `Paths.ensure()`.
`AI_Studio/nandatown/**` is in the test ignore globs because several tests
assert that the exclusion actually prunes.

No fixture here touches the network, a real Canvas token, a real calendar, or
a model.
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
    calendar:
      cli: ical-guy
      lookahead_days: 14
    email:
      sender: student@example.com
      to: student@example.com
      node: /nonexistent/node
    ignore_globs:
      - "**/.DS_Store"
      - "**/.venv/**"
      - "**/site-packages/**"
      - "**/node_modules/**"
      - "**/__pycache__/**"
      - "AI_Studio/nandatown/**"
    """
)

COURSES_YML = textwrap.dedent(
    """
    courses:
      - folder: Machine Learning
      - folder: AI_Studio
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
    # Only folders named in courses.yml are course folders (see
    # `existing_course_folders`); a test that writes its own map replaces this.
    (ws / "_agent" / "config" / "courses.yml").write_text(COURSES_YML)
    monkeypatch.setenv("MITSYNC_WORKSPACE", str(ws))
    return ws


@pytest.fixture
def settings(workspace: Path) -> Settings:
    s = load_settings(workspace / "_agent" / "config" / "settings.yml")
    # Point paths at the temp repo rather than the real one.
    s._paths = Paths(workspace=workspace, repo=workspace / "_agent")
    s.paths.ensure()
    return s

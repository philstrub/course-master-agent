"""Tests for `.env` loading (mitsync/env.py) and its effect on settings.

Everything runs against `tmp_path` fakes: `Paths(repo=...)` points the repo
`.env` at a temp dir and `HOME` is monkeypatched for `~/.openclaw/.env`, so the
user's real files and environment are never touched. No test asserts on a real
secret; the sentinel values below are fictitious.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mitsync.cli import _secret_source
from mitsync.core import env as env_mod
from mitsync.core.config import load_settings
from mitsync.core.env import (
    ENVIRONMENT,
    NOT_SET,
    OPENCLAW_DOTENV,
    REPO_DOTENV,
    load_dotenv,
    parse_dotenv,
)
from mitsync.core.paths import Paths

SECRET = "sentinel-not-a-real-token-0001"
OTHER = "sentinel-not-a-real-token-0002"
REAL = "sentinel-not-a-real-token-0003"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A temp HOME and no leftover sentinel vars."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CANVAS_TOKEN", "ANTHROPIC_API_KEY", "MITSYNC_DOTENV"):
        monkeypatch.delenv(name, raising=False)


def _repo(tmp_path: Path, body: str) -> Paths:
    repo = tmp_path / "_agent"
    repo.mkdir(exist_ok=True)
    (repo / ".env").write_text(body)
    return Paths(workspace=tmp_path, repo=repo)


def _openclaw(body: str) -> None:
    d = Path.home() / ".openclaw"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".env").write_text(body)


# --------------------------------------------------------------------------
# search order and precedence
# --------------------------------------------------------------------------
def test_value_read_from_repo_dotenv(tmp_path: Path):
    paths = _repo(tmp_path, f"CANVAS_TOKEN={SECRET}\n")
    assert load_dotenv(paths) == {"CANVAS_TOKEN": REPO_DOTENV}
    import os

    assert os.environ["CANVAS_TOKEN"] == SECRET


def test_openclaw_dotenv_used_as_fallback(tmp_path: Path):
    paths = Paths(workspace=tmp_path, repo=tmp_path / "_agent")  # no repo .env
    _openclaw(f"CANVAS_TOKEN={SECRET}\n")
    assert load_dotenv(paths) == {"CANVAS_TOKEN": OPENCLAW_DOTENV}
    import os

    assert os.environ["CANVAS_TOKEN"] == SECRET


def test_repo_dotenv_wins_over_openclaw(tmp_path: Path):
    paths = _repo(tmp_path, f"CANVAS_TOKEN={SECRET}\n")
    _openclaw(f"CANVAS_TOKEN={OTHER}\n")
    assert load_dotenv(paths) == {"CANVAS_TOKEN": REPO_DOTENV}
    import os

    assert os.environ["CANVAS_TOKEN"] == SECRET


def test_real_environment_wins_over_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CANVAS_TOKEN", REAL)
    paths = _repo(tmp_path, f"CANVAS_TOKEN={SECRET}\n")
    _openclaw(f"CANVAS_TOKEN={OTHER}\n")
    assert load_dotenv(paths) == {}
    import os

    assert os.environ["CANVAS_TOKEN"] == REAL


def test_openclaw_fills_a_key_the_repo_file_omits(tmp_path: Path):
    paths = _repo(tmp_path, f"CANVAS_TOKEN={SECRET}\n")
    _openclaw(f"ANTHROPIC_API_KEY={OTHER}\n")
    assert load_dotenv(paths) == {
        "CANVAS_TOKEN": REPO_DOTENV,
        "ANTHROPIC_API_KEY": OPENCLAW_DOTENV,
    }


# --------------------------------------------------------------------------
# doctor's provenance label, built from the mapping load_dotenv returns
# --------------------------------------------------------------------------
def test_secret_source_labels_a_dotenv_key(tmp_path: Path):
    sources = load_dotenv(_repo(tmp_path, f"CANVAS_TOKEN={SECRET}\n"))
    assert _secret_source(sources, "CANVAS_TOKEN") == REPO_DOTENV


def test_secret_source_labels_a_real_environment_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CANVAS_TOKEN", REAL)
    sources = load_dotenv(_repo(tmp_path, f"CANVAS_TOKEN={SECRET}\n"))
    assert _secret_source(sources, "CANVAS_TOKEN") == ENVIRONMENT


def test_secret_source_of_unset_variable():
    assert _secret_source({}, "MITSYNC_DEFINITELY_UNSET_VAR") == NOT_SET


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def test_parse_quoting_comments_export_whitespace_and_equals():
    parsed = parse_dotenv(
        "\n"
        "# a full-line comment\n"
        "   # an indented comment\n"
        "PLAIN=one\n"
        "  SPACED  =   two   \n"
        "export EXPORTED=three\n"
        "SINGLE='four # not a comment'\n"
        'DOUBLE="five # not a comment"\n'
        "TRAILING=six   # this is a comment\n"
        "EQUALS=a=b=c\n"
        'NEWLINE="line1\\nline2"\n'
        "EMPTY=\n"
        'QUOTED_EMPTY=""\n'
    )
    assert parsed == {
        "PLAIN": "one",
        "SPACED": "two",
        "EXPORTED": "three",
        "SINGLE": "four # not a comment",
        "DOUBLE": "five # not a comment",
        "TRAILING": "six",
        "EQUALS": "a=b=c",
        "NEWLINE": "line1\nline2",
        "EMPTY": "",
        "QUOTED_EMPTY": "",
    }


def test_malformed_lines_are_skipped_without_raising(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.DEBUG, logger="mitsync.core.env"):
        parsed = parse_dotenv(
            "NO_EQUALS_HERE\n=novalue\n1BAD=x\nBAD KEY=x\nGOOD=ok\n", origin="fake.env"
        )
    assert parsed == {"GOOD": "ok"}
    assert caplog.text.count("skipping malformed line") == 4
    assert "line 1" in caplog.text and "line 4" in caplog.text


def test_unreadable_dotenv_is_not_fatal(tmp_path: Path):
    repo = tmp_path / "_agent"
    (repo / ".env").mkdir(parents=True)  # a directory where a file is expected
    assert load_dotenv(Paths(workspace=tmp_path, repo=repo)) == {}


# --------------------------------------------------------------------------
# escape hatch and reloading
# --------------------------------------------------------------------------
def test_mitsync_dotenv_zero_disables_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MITSYNC_DOTENV", "0")
    paths = _repo(tmp_path, f"CANVAS_TOKEN={SECRET}\n")
    assert load_dotenv(paths) == {}
    import os

    assert "CANVAS_TOKEN" not in os.environ


def test_reloading_reports_the_same_mapping_and_changes_nothing(tmp_path: Path):
    paths = _repo(tmp_path, f"CANVAS_TOKEN={SECRET}\n")
    first = load_dotenv(paths)
    second = load_dotenv(paths)
    import os

    assert first == second == {"CANVAS_TOKEN": REPO_DOTENV}
    assert os.environ["CANVAS_TOKEN"] == SECRET


def test_a_second_load_never_overwrites_an_already_injected_value(tmp_path: Path):
    paths = _repo(tmp_path, f"CANVAS_TOKEN={SECRET}\n")
    load_dotenv(paths)
    (paths.repo / ".env").write_text(f"CANVAS_TOKEN={OTHER}\n")
    load_dotenv(paths)
    import os

    assert os.environ["CANVAS_TOKEN"] == SECRET


# --------------------------------------------------------------------------
# the bug this fixes
# --------------------------------------------------------------------------
def test_load_settings_triggers_dotenv_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "_agent"
    (repo / "config").mkdir(parents=True)
    (repo / ".env").write_text(f"CANVAS_TOKEN={SECRET}\n")
    monkeypatch.setattr(env_mod, "Paths", lambda: Paths(workspace=tmp_path, repo=repo))
    settings = load_settings(repo / "config" / "settings.yml")
    assert settings.canvas.token == SECRET


def test_no_secret_value_appears_in_logs_or_stdout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
):
    paths = _repo(tmp_path, f"CANVAS_TOKEN={SECRET}\nBAD LINE={OTHER}\n")
    with caplog.at_level(logging.DEBUG, logger="mitsync"):
        load_dotenv(paths)
    captured = capsys.readouterr()
    haystack = caplog.text + captured.out + captured.err
    assert SECRET not in haystack
    assert OTHER not in haystack

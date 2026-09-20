"""`.env` loading for secrets (Canvas token, LLM API key).

Why here and why called from `config.load_settings()`
-----------------------------------------------------
Every path that reads a secret goes through `Settings`: `CanvasSettings.token`
and `LLMSettings.api_key` read `os.environ` lazily, and `resolve_driver()`
depends on the latter. `load_settings()` is therefore the one choke point that
covers *all* consumers -- every `mitsync` subcommand (they all call
`cli._settings()`), `doctor`, direct library use from tests, and OpenClaw
skills that import `mitsync` without ever touching the Typer app. Wiring the
loader into the Typer `@app.callback()` alone would miss those last two, so
`load_settings()` calls `load_dotenv()` on every call (the settings object
itself is cached, this is not) and the loader is idempotent and cheap.

Semantics
---------
* Search order, each loaded if present, **first wins** for a given key:
  `<repo>/.env` then `~/.openclaw/.env`.
* The real environment always wins: a key already in `os.environ` is never
  overwritten, because OpenClaw and cron wrappers inject vars deliberately and
  a stale `.env` must not clobber them.
* `MITSYNC_DOTENV=0` disables `.env` loading entirely.
* Malformed lines are skipped with a debug log naming the line number. Values
  are never logged.
"""

from __future__ import annotations

import os
from pathlib import Path

from .logging import get_logger
from .paths import Paths

log = get_logger(__name__)

DISABLE_ENV = "MITSYNC_DOTENV"

ENVIRONMENT = "environment"
REPO_DOTENV = "_agent/.env"
OPENCLAW_DOTENV = "~/.openclaw/.env"
NOT_SET = "not set"

# Keys injected from a .env file -> the label of the file they came from.
_SOURCES: dict[str, str] = {}
_LOADED = False


def dotenv_disabled() -> bool:
    """True when `MITSYNC_DOTENV` is set to a falsy value (0/false/no/off)."""
    raw = os.environ.get(DISABLE_ENV)
    return raw is not None and raw.strip().lower() in {"0", "false", "no", "off", ""}


def parse_dotenv(text: str, origin: str = "<string>") -> dict[str, str]:
    """Parse `.env` text into a mapping. Malformed lines are skipped, not fatal."""
    out: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _valid_key(key):
            log.debug("skipping malformed line %d in %s", lineno, origin)
            continue
        out[key] = _parse_value(value.strip())
    return out


def _valid_key(key: str) -> bool:
    return bool(key) and not key[0].isdigit() and all(c == "_" or c.isalnum() for c in key)


def _parse_value(value: str) -> str:
    """Strip one layer of quotes, or an unquoted trailing `#` comment."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        return inner.replace("\\n", "\n").replace("\\t", "\t") if value[0] == '"' else inner
    head, sep, _ = value.partition(" #")
    if not sep and value.startswith("#"):
        return ""
    return head.strip()


def dotenv_files(paths: Paths | None = None) -> list[tuple[Path, str]]:
    """The candidate `.env` files, highest precedence first, with their labels."""
    p = paths or Paths()
    return [
        (p.repo / ".env", REPO_DOTENV),
        (Path.home() / ".openclaw" / ".env", OPENCLAW_DOTENV),
    ]


def load_dotenv(paths: Paths | None = None, force: bool = False) -> dict[str, str]:
    """Inject `.env` values into `os.environ` without overwriting real env vars.

    Idempotent: after the first call this returns the recorded sources without
    re-reading anything, unless `force=True`. Returns `{key: source label}` for
    the keys that came from a file.
    """
    global _LOADED
    if _LOADED and not force:
        return dict(_SOURCES)
    _LOADED = True
    if dotenv_disabled():
        _SOURCES.clear()
        return {}
    injected: dict[str, str] = {}
    for path, label in dotenv_files(paths):
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.debug("could not read %s: %s", path, type(exc).__name__)
            continue
        for key, value in parse_dotenv(text, origin=str(path)).items():
            if key in os.environ or key in injected:
                continue  # real environment, and earlier files, win
            os.environ[key] = value
            injected[key] = label
    _SOURCES.clear()
    _SOURCES.update(injected)
    return dict(injected)


def source_of(name: str) -> str:
    """Where `name`'s current value came from: a `.env` label, `environment`, or `not set`."""
    if not os.environ.get(name):
        return NOT_SET
    return _SOURCES.get(name, ENVIRONMENT)


def reset() -> None:
    """Forget that a load happened (tests only; does not unset variables)."""
    global _LOADED
    _LOADED = False
    _SOURCES.clear()

"""
# Dotenv Loading

Where mitsync's secrets come from, and why the real environment always wins.

## 1. What This Module Does

Finds `.env` files, parses them, and injects the keys that are not already set
into `os.environ`. It returns a `{key: source label}` mapping so that whoever
wants to report a secret's provenance -- `mitsync doctor` -- can, without the
value ever being logged.

## 2. Why This Module Exists

The two secrets mitsync uses (a Canvas token and an LLM API key) are read
lazily off `os.environ` by `Settings`, which is what keeps them out of every
dump, task file and log line. Something still has to put them there for a user
who does not want to export variables by hand, and that something must run for
*every* consumer: each subcommand, `doctor`, tests using the library directly,
and OpenClaw skills that import `mitsync` without ever touching the Typer app.
Wiring the loader into the Typer callback would have missed the last two, so
`config.load_settings()` calls this on every invocation instead.

## 3. How It Fits in the Architecture

A near-leaf, below `config` and imported by it and by `cli doctor`. It holds no
state of its own: re-running the loader is cheap, injects nothing twice, and
reports the same mapping.

## 4. Key Concepts

**Search order, first wins.** `<repo>/.env`, then `~/.openclaw/.env`.

**The real environment always wins.** A key already present in `os.environ` is
never overwritten, because OpenClaw wrappers and cron jobs inject variables
deliberately and a stale `.env` must not clobber them. `MITSYNC_DOTENV=0`
disables the whole mechanism.

**Values are never logged.** A malformed line is skipped with a debug message
naming the line number only.
"""

from __future__ import annotations

import os
from pathlib import Path

from mitsync.core.logging import get_logger
from mitsync.core.paths import Paths

log = get_logger(__name__)

DISABLE_ENV = "MITSYNC_DOTENV"

ENVIRONMENT = "environment"
REPO_DOTENV = "_agent/.env"
OPENCLAW_DOTENV = "~/.openclaw/.env"
NOT_SET = "not set"


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


def load_dotenv(paths: Paths | None = None) -> dict[str, str]:
    """Inject `.env` values into `os.environ` without overwriting real env vars.

    Returns `{key: source label}` for every key a `.env` file supplied whose
    value is the one now live in `os.environ` -- so a key the real environment
    already held (with a different value) is absent, and a repeated call reports
    the same mapping without changing anything.
    """
    if dotenv_disabled():
        return {}
    sources: dict[str, str] = {}
    for path, label in dotenv_files(paths):
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.debug("could not read %s: %s", path, type(exc).__name__)
            continue
        for key, value in parse_dotenv(text, origin=str(path)).items():
            if key in sources:
                continue  # an earlier, higher-precedence file already supplied it
            if key not in os.environ:
                os.environ[key] = value  # the real environment always wins
            if os.environ[key] == value:
                sources[key] = label
    return sources

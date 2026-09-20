"""
# Configuration

`config/settings.yml`, validated once into the `Settings` object every command
shares.

## 1. What This Module Does

Loads and validates the settings file into a pydantic tree (Canvas, LLM,
calendar, graph, organize, ignore globs), decides which judgment driver a run
will use, and answers "is this path excluded?" for every walk in the tool. It
also holds the two readers for JSON that mitsync itself wrote -- `read_json`
and `read_meta`.

## 2. Why This Module Exists

Validation happens at the boundary so business logic can trust its inputs: a
typo'd key or an unsupported provider fails at load with the file named, not
three modules later as an `AttributeError`. The file is entirely optional --
every field has a default -- because mitsync must work on a fresh checkout with
no configuration at all.

Secrets deliberately are not part of the model. `CanvasSettings.token` and
`LLMSettings.api_key` read `os.environ` lazily, so a token can never be
serialized into a settings dump, a task file, or a log line.

## 3. How It Fits in the Architecture

Every command starts at `cli._settings()`, so `load_settings()` is the one
choke point covering all consumers -- including tests and OpenClaw skills that
import the library and never touch Typer. It therefore calls
`env.load_dotenv()` on every call: the `Settings` object is cached, that call
is not, so a cache hit still sees a `.env`.

`read_meta` lives here rather than next to its writer in `sync.py` so that
readers of the mirror metadata (`deadlines`, `course_map`) can parse a local
JSON file without importing the Canvas sync machinery, and therefore `httpx`.

## 4. Key Concepts

**Driver resolution is the whole policy.** `resolve_driver()`: an explicit
`--driver` flag beats `settings.llm.driver`, and `auto` means `api` when an API
key is present and `agent` otherwise. Zero credentials is a supported default,
not a degraded mode.

**Ignore globs are a guardrail, not a preference.** `DEFAULT_IGNORE_GLOBS`
lives in Python and not only in the YAML because the YAML is optional: a
missing settings file must not silently switch off the exclusion of `.venv`,
`site-packages`, `node_modules` and `nandatown`. Patterns are gitignore-style,
where `**` crosses directory separators and `*` does not.

**Why exceptions are caught here.** Three handlers, all of them translating a
foreign failure into a typed error that names the file. `yaml.YAMLError` and
pydantic's `ValidationError` become `ConfigError`; `json.JSONDecodeError`
becomes a `MitsyncError` carrying the path, because the stdlib error reports a
line and column but not a filename, which is useless to an operator holding a
dozen state files. Corrupt config and corrupt state are never absorbed into an
empty default -- they raise.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from mitsync.core.env import load_dotenv
from mitsync.core.errors import ConfigError, MitsyncError
from mitsync.core.paths import Paths

Driver = Literal["api", "agent", "rules"]
_DRIVERS: tuple[str, ...] = ("api", "agent", "rules")


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-style glob into a regex matching a whole path.

    `**` spans directory separators, `*` and `?` do not. A leading `**/` is
    optional, so `**/.DS_Store` also matches `.DS_Store` at the root.
    """
    out = ["^"]
    if pattern.startswith("**/"):
        out.append("(?:.*/)?")
        pattern = pattern[3:]
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("/**", i):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return re.compile("".join(out))


class CanvasSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://canvas.mit.edu/api/v1"
    token_env: str = "CANVAS_TOKEN"
    per_page: int = 100
    term: str = "auto"
    #: Courses never synced: a Canvas course id (int or numeric string), or a
    #: case-insensitive substring of the course name or course_code.
    exclude_courses: list[int | str] = Field(default_factory=list)
    max_retries: int = 5
    min_rate_limit_remaining: float = 100.0

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env) or None


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver: Literal["auto", "api", "agent", "rules"] = "auto"
    provider: Literal["anthropic", "openai", "google", "openai_compatible"] = "anthropic"
    model: str = "claude-opus-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str | None = None
    max_output_tokens: int = 8000
    temperature: float = 0.0

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


class CalendarSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cli: str = "ical-guy"
    lookahead_days: int = 14


class GraphSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["duckdb"] = "duckdb"


class OrganizeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    link_mode: Literal["hardlink", "copy", "symlink"] = "hardlink"


DEFAULT_IGNORE_GLOBS = [
    "**/.DS_Store",
    "**/.venv/**",
    "**/site-packages/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    # Guardrail 5 in CLAUDE.md. It lives here, not only in config/settings.yml,
    # because settings.yml is optional -- a missing file must not silently turn
    # the most-repeated exclusion in this repo off.
    "**/nandatown/**",
]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canvas: CanvasSettings = Field(default_factory=CanvasSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    calendar: CalendarSettings = Field(default_factory=CalendarSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    organize: OrganizeSettings = Field(default_factory=OrganizeSettings)
    ignore_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE_GLOBS))

    # Not part of the YAML: where this instance was loaded from / operates on.
    _source_path: Path | None = PrivateAttr(default=None)
    _paths: Paths = PrivateAttr(default=None)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    @property
    def paths(self) -> Paths:
        if self._paths is None:
            self._paths = Paths()
        return self._paths

    def resolve_driver(self, cli_override: str | None = None) -> Driver:
        """Explicit flag wins; else settings; `auto` -> api if a key is set, else agent."""
        chosen = cli_override or self.llm.driver
        if chosen not in (*_DRIVERS, "auto"):
            raise ConfigError(f"unknown llm driver {chosen!r}; expected one of {_DRIVERS} or auto")
        if chosen == "auto":
            return "api" if self.llm.api_key else "agent"
        return chosen  # type: ignore[return-value]

    def should_ignore(self, relpath: str | Path) -> bool:
        """True if a workspace-relative path matches any ignore glob."""
        rel = str(relpath).replace(os.sep, "/")
        while rel.startswith("./"):
            rel = rel[2:]
        rel = rel.lstrip("/")
        if not rel:
            return False
        return any(rx.match(rel) for rx in self._ignore_regexes())

    def _ignore_regexes(self) -> tuple[re.Pattern[str], ...]:
        return _compile_globs(tuple(self.ignore_globs))


@lru_cache(maxsize=32)
def _compile_globs(globs: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(_glob_to_regex(g) for g in globs)


def read_json(path: Path) -> Any:
    """Read a JSON file mitsync itself wrote, naming the file if it is corrupt.

    `json.JSONDecodeError` carries a line and column but not a filename, which
    is useless to an operator holding a dozen state files. Corrupt state is a
    real failure -- it is never absorbed into an empty default.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MitsyncError(f"{path} is not valid JSON: {exc}") from exc


def _read_yaml(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return raw


@lru_cache(maxsize=8)
def _load_cached(path_str: str | None) -> Settings:
    paths = Paths()
    path = Path(path_str) if path_str else paths.settings_file
    data = _read_yaml(path) if path.exists() else {}
    try:
        settings = Settings(**data)
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{exc}") from exc
    settings._source_path = path
    settings._paths = paths
    return settings


def load_settings(path: Path | None = None) -> Settings:
    """Load settings (cached per path). Missing file -> all defaults.

    Loads `.env` first (idempotent, see `mitsync.env`) so that secrets read
    lazily off `os.environ` -- the Canvas token, the LLM key, and therefore
    `resolve_driver()` -- see values placed in a `.env` file. The settings
    object is cached; this call is not, so it also covers a cache hit.
    """
    load_dotenv()
    return _load_cached(str(path) if path else None)


def clear_cache() -> None:
    """Drop the settings cache; tests and `doctor --reload` use this."""
    _load_cached.cache_clear()


def read_meta(path: Path) -> list[dict[str, Any]]:
    """The ``items`` array of one ``_canvas/<course>/_meta/*.json`` file.

    Lives here rather than beside the writer in ``sync.py`` so that readers of
    the mirror metadata (``deadlines``, ``course_map``) do not have to import
    the Canvas sync machinery -- and therefore ``httpx`` -- to parse a local
    JSON file. ``sync`` re-exports it so the format stays documented next to
    the code that writes it.

    A corrupt file raises (``read_json``); a *missing* one is the caller's
    call, because only the caller knows whether it is a gap to report or a bug.
    """
    doc = read_json(path)
    items = doc.get("items") if isinstance(doc, dict) else doc
    return [i for i in items or [] if isinstance(i, dict)]

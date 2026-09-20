"""Settings loaded from config/settings.yml, validated with pydantic v2."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from .env import load_dotenv
from .errors import ConfigError
from .paths import Paths

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


class Neo4jSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str | None = None
    user: str | None = None
    password_env: str | None = None


class GraphSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["duckdb", "neo4j"] = "duckdb"
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)


class OrganizeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_existing: bool = False
    link_mode: Literal["hardlink", "copy", "symlink"] = "hardlink"


DEFAULT_IGNORE_GLOBS = [
    "**/.DS_Store",
    "**/.venv/**",
    "**/site-packages/**",
    "**/node_modules/**",
    "**/__pycache__/**",
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

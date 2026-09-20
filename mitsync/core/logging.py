"""
# Logging

One rich handler on the `mitsync` logger tree, installed once, by the CLI.

## 1. What This Module Does

`setup_logging()` attaches a single `rich` handler to the `mitsync` logger and
stops propagation to the root logger. `get_logger()` hands each module its own
child logger under that tree.

## 2. Why This Module Exists

mitsync is a CLI *and* a library imported by tests and by OpenClaw skills.
Configuring the root logger would mean a mere import changing logging for the
whole host process; adding a second handler would print every line twice.
Installing exactly one handler on one named tree, from the CLI entry point
only, avoids both. A consumer that never calls `setup_logging()` gets ordinary
quiet loggers, which is the correct default for a library.

## 3. How It Fits in the Architecture

A leaf module. Every other module does `log = get_logger(__name__)` at import
time; only `cli.main()` calls `setup_logging()`, and `--verbose` is the only
thing that changes about it.

## 4. Key Concepts

**The logger name is the attribution.** Messages are never hand-prefixed with
a function or module name -- the handler already prints the logger name.

**Logs go to stderr.** The console here is stderr-bound so that report output
on stdout stays pipeable and machine-readable.
"""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

console = Console(stderr=True)
_ROOT = "mitsync"


def setup_logging(verbose: bool = False) -> None:
    """Install a single rich handler on the mitsync logger tree."""
    logger = logging.getLogger(_ROOT)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.addHandler(
        RichHandler(
            console=console,
            show_path=verbose,
            show_time=False,
            rich_tracebacks=True,
            markup=False,
        )
    )


def get_logger(name: str) -> logging.Logger:
    """Logger for a module; `name` may be a bare module name or dotted path."""
    suffix = name.removeprefix(f"{_ROOT}.")
    return logging.getLogger(_ROOT if suffix == _ROOT else f"{_ROOT}.{suffix}")

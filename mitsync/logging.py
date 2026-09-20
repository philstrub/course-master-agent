"""Rich-backed logging. Call setup_logging() once from the CLI."""

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

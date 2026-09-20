"""macOS notifications, on a strictly best-effort basis.

`terminal-notifier` if it is installed, otherwise `osascript`. Never raises:
a notification that cannot be shown is not a reason for a sync to fail, so
every failure path returns ``False`` and logs. Set ``MITSYNC_NO_NOTIFY`` to
silence it entirely (CI, cron, tests).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .logging import get_logger

log = get_logger(__name__)

__all__ = ["notify"]

SILENCE_ENV = "MITSYNC_NO_NOTIFY"
TIMEOUT_SECONDS = 10


def _applescript_quote(text: str) -> str:
    """Escape a Python string for embedding in an AppleScript string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _silenced() -> bool:
    if os.environ.get(SILENCE_ENV):
        log.debug("notifications silenced by $%s", SILENCE_ENV)
        return True
    return False


def notify(title: str, message: str, *, subtitle: str | None = None) -> bool:
    """Show a desktop notification. Returns ``True`` only if one was shown."""
    if _silenced():
        return False

    title, message = str(title), str(message)
    notifier = shutil.which("terminal-notifier")
    if notifier is None and sys.platform != "darwin":
        log.debug("no notifier and not on macOS; skipping notification %r", title)
        return False
    if notifier is None and not sys.stdout.isatty() and shutil.which("osascript") is None:
        log.debug("no notifier available and no tty; skipping notification %r", title)
        return False

    if notifier:
        argv = [notifier, "-title", title, "-message", message]
        if subtitle:
            argv += ["-subtitle", subtitle]
    else:
        osascript = shutil.which("osascript")
        if osascript is None:
            log.debug("osascript is not available; skipping notification %r", title)
            return False
        script = f'display notification "{_applescript_quote(message)}"'
        script += f' with title "{_applescript_quote(title)}"'
        if subtitle:
            script += f' subtitle "{_applescript_quote(subtitle)}"'
        argv = [osascript, "-e", script]

    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("notification failed (%s): %s", type(exc).__name__, exc)
        return False
    except Exception as exc:  # pragma: no cover -- notify must never propagate
        log.debug("unexpected notification failure: %s", exc)
        return False

    if proc.returncode != 0:
        log.debug("notifier exited %s: %s", proc.returncode, (proc.stderr or "").strip())
        return False
    return True

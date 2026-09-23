"""
# Email brief

Render the agent's morning brief as a dashboard and email it to the student.

## 1. What This Module Does

`send_brief` reads `_kb/briefings/<date>-morning.json` (written by the agent,
validated against `email/brief.schema.json`), adds the facts the agent should
not be trusted to copy (today's classes from the calendar, Canvas sync
freshness), renders it through the React Email template in `email/` into
`<date>-morning.html` and `.txt`, and sends both as one multipart email over
SMTP.

## 2. Why This Module Exists

The judgment (what matters, how far along each homework is) belongs to the
agent. Getting that judgment to the student is I/O: validate, render, send,
exactly once. Keeping it here means a scheduled run cannot send a malformed
email, a duplicate, or a message to anyone but the student.

## 3. How It Fits in the Architecture

Sits beside `deadlines` in `schedule/`: it reads the calendar and the
manifest's last sync, and shells out to Node only to render. It imports
nothing from `canvas/` or `filing/`.

## 4. Key Concepts

**The recipient is configuration, never data.** `email.to` and `email.sender`
come from `config/settings.yml`, and the SMTP password from the environment
(`email.password_env`, default `GMAIL_APP_PASSWORD`, usually in `_agent/.env`).
Nothing in the brief JSON can change where the email goes, so a Canvas page
that talks the agent into writing "send this to x@y" achieves nothing.

**Once per day.** A successful send writes `state/sent/<date>.json`; a second
call for the same date refuses unless `resend=True`. Cron retries and a
repeated chat request therefore cannot spam the inbox.

**Render before send, always.** The HTML is written to `_kb/briefings/` first,
so `--dry-run` produces the exact dashboard that would be sent, and a render
failure stops before any network call.
"""

from __future__ import annotations

import json
import os
import shutil
import smtplib
import ssl
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mitsync.core.errors import CalendarAccessDenied, ConfigError, MitsyncError
from mitsync.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from mitsync.core.config import Settings

log = get_logger(__name__)

__all__ = ["EmailError", "SendResult", "enrich", "send_brief", "validate_brief"]

RENDER_TIMEOUT_SECONDS = 60
FRESH_HOURS = 24


class EmailError(MitsyncError):
    """The brief could not be validated, rendered or sent."""


class SendResult(dict):
    """`{"html": Path, "text": Path, "sent": bool, "to": str, "message_id": str | None}`."""


def _email_dir(settings: Settings) -> Path:
    return settings.paths.repo / "email"


def validate_brief(settings: Settings, brief: Any) -> dict[str, Any]:
    """Validate the agent's JSON against `email/brief.schema.json`."""
    import jsonschema

    schema = json.loads((_email_dir(settings) / "brief.schema.json").read_text(encoding="utf-8"))
    errors = sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(brief),
        key=lambda e: list(e.absolute_path),
    )
    if errors:
        lines = [
            f"  at {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors[:10]
        ]
        raise EmailError("the morning brief failed validation:\n" + "\n".join(lines))
    return brief


def _classes_today(settings: Settings, day: datetime) -> tuple[list[dict[str, Any]], str | None]:
    """Course-tagged, timed events for `day` (local). Personal events stay out.

    The CLI's `--from`/`--to` are *inclusive* days, so the query asks for
    `day` to `day`; asking for `day` to `day + 1` once put Thursday's classes
    in Wednesday's timetable. Events are also filtered to `day` here, so a
    CLI with exclusive ends, or a multi-day event, cannot leak into it.
    """
    from mitsync.schedule import calendar as calendar_read

    ok, detail = calendar_read.calendar_available(settings)
    if not ok:
        return [], f"calendar unavailable: {detail}"
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        events = calendar_read.read_events(settings, start, start)
    except CalendarAccessDenied as exc:
        return [], f"calendar access denied: {str(exc).splitlines()[0]}"
    except MitsyncError as exc:
        return [], f"calendar unavailable: {exc}"
    out = []
    for event in events:
        if not event.course or event.all_day:
            continue
        try:
            begins = datetime.fromisoformat(event.start.replace("Z", "+00:00")).astimezone()
            ends = datetime.fromisoformat(event.end.replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        if begins.date() != start.date():
            continue
        out.append(
            {
                "time": begins.strftime("%H:%M"),
                "end": ends.strftime("%H:%M"),
                "title": event.title,
                "course": event.course,
                "location": event.location,
            }
        )
    return sorted(out, key=lambda c: c["time"]), None


def enrich(
    settings: Settings, brief: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """Add the facts: today's classes, sync freshness, generation time."""
    from mitsync.schedule.deadlines import last_sync

    now = now or datetime.now(UTC)
    classes, gap = _classes_today(settings, now.astimezone())
    last = last_sync(settings)
    fresh = False
    if last:
        synced = datetime.fromisoformat(last.replace("Z", "+00:00"))
        fresh = now - synced < timedelta(hours=FRESH_HOURS)
    gaps = list(brief["gaps"])
    if gap and gap not in gaps:
        gaps.append(gap)
    if not fresh:
        gaps.insert(0, "Canvas has not synced in the last 24 h: deadlines may be out of date.")
    return {
        **brief,
        "gaps": gaps,
        "classes": classes,
        "sync": {"last": last, "fresh": fresh},
        "generated_at": now.isoformat(timespec="seconds"),
    }


def _node(settings: Settings) -> str:
    node = settings.email.node or shutil.which("node")
    if not node or not Path(node).exists() and not shutil.which(node):
        raise ConfigError(
            "Node.js is needed to render the email; set email.node in config/settings.yml "
            "to its absolute path (`command -v node`)."
        )
    return node


def render(settings: Settings, brief: dict[str, Any], html: Path, text: Path) -> None:
    """Render the enriched brief with `email/render.tsx` (React Email)."""
    edir = _email_dir(settings)
    tsx = edir / "node_modules" / "tsx" / "dist" / "cli.mjs"
    if not tsx.exists():
        raise ConfigError(f"email renderer not installed: run `npm install` in {edir}")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(brief, fh, ensure_ascii=False)
        payload = Path(fh.name)
    try:
        proc = subprocess.run(
            [_node(settings), str(tsx), "render.tsx", str(payload), str(html), str(text)],
            cwd=edir,
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EmailError(f"rendering took longer than {RENDER_TIMEOUT_SECONDS}s") from exc
    finally:
        payload.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise EmailError(f"rendering failed:\n{(proc.stderr or proc.stdout).strip()[-2000:]}")


def _message(settings: Settings, brief: dict[str, Any], html: str, text: str) -> EmailMessage:
    day = datetime.fromisoformat(brief["date"]).strftime("%a %d %b")
    msg = EmailMessage()
    msg["Subject"] = f"Morning brief · {day} — {brief['headline']}"[:180]
    msg["From"] = settings.email.sender
    msg["To"] = settings.email.to
    msg["Message-ID"] = make_msgid(domain="mitsync.local")
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send_brief(
    settings: Settings,
    date: str | None = None,
    *,
    dry_run: bool = False,
    resend: bool = False,
) -> SendResult:
    """Validate, enrich, render and (unless `dry_run`) email one morning brief."""
    date = date or datetime.now().strftime("%Y-%m-%d")
    source = settings.paths.kb_briefings / f"{date}-morning.json"
    if not source.exists():
        raise EmailError(f"no brief to send: the agent has not written {source}")
    try:
        brief = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EmailError(f"{source} is not valid JSON: {exc}") from exc
    validate_brief(settings, brief)
    if brief["date"] != date:
        raise EmailError(f"{source.name} says date {brief['date']!r}, expected {date!r}")

    html_path = source.with_suffix(".html")
    text_path = source.with_suffix(".txt")
    render(settings, enrich(settings, brief), html_path, text_path)
    result = SendResult(html=html_path, text=text_path, sent=False, to=settings.email.to)
    if dry_run:
        return result

    marker = settings.paths.state_dir / "sent" / f"{date}.json"
    if marker.exists() and not resend:
        raise EmailError(f"the brief for {date} was already sent ({marker}); pass --resend")
    if not settings.email.to or not settings.email.sender:
        raise ConfigError("set email.sender and email.to in config/settings.yml")
    password = os.environ.get(settings.email.password_env)
    if not password:
        raise ConfigError(
            f"${settings.email.password_env} is not set. Create a Gmail app password "
            "(Google Account → Security → App passwords) and put it in _agent/.env."
        )

    msg = _message(settings, brief, html_path.read_text("utf-8"), text_path.read_text("utf-8"))
    try:
        with smtplib.SMTP_SSL(
            settings.email.smtp_host, settings.email.smtp_port, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(settings.email.sender, password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"sending failed: {exc}") from exc

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "to": settings.email.to,
                "message_id": msg["Message-ID"],
                "sent_at": datetime.now(UTC).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    log.info("emailed the %s brief to %s", date, settings.email.to)
    result.update(sent=True, message_id=msg["Message-ID"])
    return result

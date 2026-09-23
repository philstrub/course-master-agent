"""email_brief.py: validate, enrich, render and send the morning brief."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from mitsync.core.config import Settings
from mitsync.core.paths import REPO_ROOT
from mitsync.schedule import email_brief

REAL_EMAIL_DIR = REPO_ROOT / "email"
DATE = "2026-09-23"


def _brief(**overrides) -> dict:
    brief = {
        "date": DATE,
        "headline": "Submit Analytics Edge A1 first.",
        "homework": [
            {
                "course": "Analytics Edge",
                "title": "Assignment 1",
                "due_at": "2026-09-24T03:59:59Z",
                "urgency": "now",
                "status": "ready_to_submit",
                "progress": 100,
                "summary": "Every part answered.",
                "next_step": "Submit the PDF.",
                "review": [],
                "effort_hours": 0.1,
            }
        ],
        "gaps": [],
    }
    brief.update(overrides)
    return brief


@pytest.fixture
def ready(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """The schema in the temp repo, a brief on disk, no calendar, a password set."""
    (settings.paths.repo / "email").symlink_to(REAL_EMAIL_DIR, target_is_directory=True)
    (settings.paths.kb_briefings / f"{DATE}-morning.json").write_text(json.dumps(_brief()))
    monkeypatch.setenv("PATH", str(tmp_path / "no-calendar-cli"))  # node found via email.node
    monkeypatch.setenv(settings.email.password_env, "app-password")
    monkeypatch.setattr(email_brief, "render", _fake_render)
    return settings


def _fake_render(settings, brief, html: Path, text: Path) -> None:
    html.write_text(f"<html>{brief['headline']}</html>")
    text.write_text(brief["headline"])


class FakeSMTP:
    sent: list[EmailMessage] = []

    def __init__(self, host: str, port: int, context=None) -> None:
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        assert password == "app-password"

    def send_message(self, msg: EmailMessage) -> None:
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.sent = []
    monkeypatch.setattr(email_brief.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_schema_rejects_a_brief_with_a_bad_field(ready: Settings) -> None:
    bad = _brief()
    bad["homework"][0]["urgency"] = "whenever"
    with pytest.raises(email_brief.EmailError, match="homework/0/urgency"):
        email_brief.validate_brief(ready, bad)


def test_schema_rejects_a_recipient_smuggled_into_the_brief(ready: Settings) -> None:
    with pytest.raises(email_brief.EmailError, match="Additional properties"):
        email_brief.validate_brief(ready, _brief(to="attacker@example.com"))


def test_enrich_flags_a_stale_sync_and_an_unavailable_calendar(ready: Settings) -> None:
    doc = email_brief.enrich(ready, _brief(), now=datetime(2026, 9, 23, 11, 30, tzinfo=UTC))
    assert doc["sync"] == {"last": None, "fresh": False}
    assert doc["classes"] == []
    assert doc["gaps"][0].startswith("Canvas has not synced")
    assert any("calendar unavailable" in g for g in doc["gaps"])


def test_dry_run_renders_without_sending(ready: Settings, smtp) -> None:
    result = email_brief.send_brief(ready, DATE, dry_run=True)
    assert result["html"].read_text() == "<html>Submit Analytics Edge A1 first.</html>"
    assert result["sent"] is False
    assert smtp.sent == []


def test_send_goes_to_the_configured_address_once(ready: Settings, smtp) -> None:
    result = email_brief.send_brief(ready, DATE)

    assert result["sent"] is True
    (msg,) = smtp.sent
    assert msg["To"] == ready.email.to
    assert msg["From"] == ready.email.sender
    assert msg["Subject"].startswith("Morning brief · Wed 23 Sep")
    assert msg.get_body(("html",)).get_content().startswith("<html>")

    with pytest.raises(email_brief.EmailError, match="already sent"):
        email_brief.send_brief(ready, DATE)
    email_brief.send_brief(ready, DATE, resend=True)
    assert len(smtp.sent) == 2


def test_missing_password_stops_before_any_network_call(
    ready: Settings, smtp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ready.email.password_env)
    with pytest.raises(email_brief.ConfigError, match="app password"):
        email_brief.send_brief(ready, DATE)
    assert smtp.sent == []


def test_missing_brief_file_is_a_clear_error(settings: Settings) -> None:
    with pytest.raises(email_brief.EmailError, match="has not written"):
        email_brief.send_brief(settings, "2026-01-01", dry_run=True)


@pytest.mark.skipif(
    not (REAL_EMAIL_DIR / "node_modules").exists() or not shutil.which("node"),
    reason="email renderer not installed (npm install in _agent/email)",
)
def test_real_render_produces_the_dashboard(settings: Settings, tmp_path: Path) -> None:
    (settings.paths.repo / "email").symlink_to(REAL_EMAIL_DIR, target_is_directory=True)
    settings.email.node = shutil.which("node")
    doc = email_brief.enrich(settings, _brief(), now=datetime(2026, 9, 23, 11, 30, tzinfo=UTC))
    html, text = tmp_path / "b.html", tmp_path / "b.txt"
    email_brief.render(settings, doc, html, text)
    body = html.read_text()
    assert "Submit Analytics Edge A1 first." in body
    assert "Ready to submit" in body
    assert "Ready to submit" in text.read_text()

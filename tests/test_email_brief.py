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
    assert doc["schedule"] == []
    assert doc["gaps"][0].startswith("Canvas has not synced")
    assert any("calendar unavailable" in g for g in doc["gaps"])


def _mirrored(settings: Settings, uuid: str, name: str, first_seen: str, **kw) -> None:
    from mitsync.canvas.manifest import FileRecord, Manifest

    with Manifest(settings.paths.manifest_db) as man:
        man.upsert_file(
            FileRecord(
                uuid=uuid, canvas_id=1, course_folder="Optimization", course_canvas_id=1,
                display_name=name, filename=name, content_type="application/pdf", size=1,
                canvas_folder=None, module_name="Week 5", module_position=1, updated_at="",
                sha256="x", mirror_path=f"_canvas/Optimization/{name}",
                filed_path=kw.get("filed"), first_seen=first_seen, last_synced=first_seen,
                skip_reason=kw.get("skip"),
            )
        )  # fmt: skip


def test_new_files_are_those_mirrored_since_the_last_brief_went_out(ready: Settings) -> None:
    sent = ready.paths.state_dir / "sent"
    sent.mkdir(parents=True)
    (sent / "2026-09-18.json").write_text(json.dumps({"sent_at": "2026-09-18T11:00:00+00:00"}))
    _mirrored(ready, "old", "L4.pdf", "2026-09-17T10:00:00+00:00")
    filed = "Optimization/lectures/L5.pdf"
    _mirrored(ready, "new", "L5.pdf", "2026-09-20T10:00:00+00:00", filed=filed)
    _mirrored(ready, "pre", "Pre.pdf", "2026-09-21T10:00:00+00:00", skip="PostClass filed")
    _mirrored(ready, "hw", "HW3.pdf", "2026-09-23T10:00:00+00:00")

    doc = email_brief.enrich(ready, _brief(), now=datetime(2026, 9, 23, 11, 30, tzinfo=UTC))

    new = doc["new_files"]
    assert new["since"] == "2026-09-18T11:00:00+00:00"  # Friday's brief, on a Monday-like gap
    assert [(f["title"], f["filed"]) for f in new["items"]] == [
        ("HW3.pdf", None),
        ("L5.pdf", "Optimization/lectures/L5.pdf"),
    ]
    assert new["items"][0] == {**new["items"][0], "course": "Optimization", "module": "Week 5"}
    assert new["more"] == 0


def test_with_no_brief_sent_new_files_cover_the_last_day(ready: Settings) -> None:
    _mirrored(ready, "a", "old.pdf", "2026-09-21T10:00:00+00:00")
    _mirrored(ready, "b", "fresh.pdf", "2026-09-23T01:00:00+00:00")
    doc = email_brief.enrich(ready, _brief(), now=datetime(2026, 9, 23, 11, 30, tzinfo=UTC))
    assert [f["title"] for f in doc["new_files"]["items"]] == ["fresh.pdf"]


def test_a_reading_card_validates(ready: Settings) -> None:
    hw = {**_brief()["homework"][0], "kind": "reading", "title": "Read: Moderna (A)"}
    email_brief.validate_brief(ready, _brief(homework=[hw]))
    with pytest.raises(email_brief.EmailError, match="kind"):
        email_brief.validate_brief(ready, _brief(homework=[{**hw, "kind": "quiz"}]))


def test_schedule_is_today_only(ready: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the inclusive `--to` once merged Thursday into Wednesday."""
    from mitsync.schedule import calendar as calendar_read

    asked: list[tuple[datetime, datetime]] = []

    def event(start: str, end: str, title: str, **kw) -> calendar_read.Event:
        return calendar_read.Event(start=start, end=end, title=title, calendar="c", **kw)

    def events(settings, start, end):
        asked.append((start, end))
        return [
            event(
                "2026-09-23T12:30:00", "2026-09-23T14:00:00", "Wed", course="Opt", location="E51"
            ),
            event("2026-09-23T20:25:00", "2026-09-24T03:15:00", "Flight"),
            event("2026-09-23T00:00:00", "2026-09-24T00:00:00", "Due", all_day=True),
            event("2026-09-24T12:30:00", "2026-09-24T14:00:00", "Thu", course="Opt"),
            event("2026-09-24T00:00:00", "2026-09-29T00:00:00", "Trip", all_day=True),
        ]

    monkeypatch.setattr(calendar_read, "calendar_available", lambda s: (True, "fake"))
    monkeypatch.setattr(calendar_read, "read_events", events)
    schedule, gap = email_brief._schedule_today(ready, datetime(2026, 9, 23, 7, 0))

    assert gap is None
    assert [c["title"] for c in schedule] == ["Due", "Wed", "Flight"]
    assert schedule[1] == {
        "start": "12:30",
        "end": "14:00",
        "title": "Wed",
        "course": "Opt",
        "location": "E51",
        "calendar": "c",
        "all_day": False,
        "ends_next_day": False,
    }
    assert schedule[2]["ends_next_day"] is True
    assert asked[0][0].date() == asked[0][1].date()


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
    reading = {
        **_brief()["homework"][0], "kind": "reading", "course": "From Anaytics to Action",
        "title": "Read: Moderna (A)", "status": "not_started", "progress": None,
    }  # fmt: skip
    brief = _brief(homework=[*_brief()["homework"], reading])
    doc = email_brief.enrich(settings, brief, now=datetime(2026, 9, 23, 11, 30, tzinfo=UTC))
    doc["new_files"] = {
        "since": "2026-09-22T11:00:00+00:00",
        "items": [
            {"course": "Optimization", "title": "Fall_2026_15_C57-L7.pdf",
             "filed": "Optimization/lectures/L7.pdf", "module": "Week 5"},
            {"course": "From Anaytics to Action", "title": "Moderna (A)",
             "filed": None, "module": "Class 4"},
        ],
        "more": 3,
    }  # fmt: skip
    html, text = tmp_path / "b.html", tmp_path / "b.txt"
    email_brief.render(settings, doc, html, text)
    body = html.read_text()
    assert "Submit Analytics Edge A1 first." in body
    assert "Ready to submit" in body
    assert "Ready to submit" in text.read_text()
    assert "Required reading" in body and "To read" in body
    plain = text.read_text()
    assert "New on Canvas" in plain
    assert "L7.pdf" in plain and "lectures" in plain
    assert "Moderna (A)" in plain and "not filed yet · Class 4" in plain
    assert "and 3 more" in plain

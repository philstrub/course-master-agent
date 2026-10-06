"""
# HBS Publishing cases

Download a Harvard Business Publishing case or article that a Canvas module
links to, as the student would by clicking it and pressing "Download PDF".

## 1. What This Module Does

`download_case` takes a module item of type `ExternalTool` whose
`external_url` is on `services.hbsp.harvard.edu` and writes the PDF to a path:

1. asks Canvas for a one-time launch URL (`GET .../sessionless_launch`, read
   only, with the student's token);
2. opens it, which yields Canvas's signed LTI 1.1 launch form for HBS;
3. submits that form to HBS, which starts an HBS session for the student and
   shows the item page;
4. submits the item page's `pdfLaunch` form, which answers with the PDF.

## 2. Why This Module Exists

The course pack is licensed to the student through Canvas, and HBS lets the
student download each item. The Canvas token alone cannot reach the bytes:
they sit behind the LTI launch, so this module performs it.

## 3. How It Fits in the Architecture

Called by `sync` for each HBS module link, beside the Google Slides export.
It uses the `CanvasClient` only for the read-only launch request, and its own
cookie-keeping `httpx.Client` (over the same transport) for HBS.

## 4. Key Concepts

**The only HTTP POSTs in mitsync, and only to HBS.** An LTI launch is a signed
form POST: it is a login, not a write to anything graded. `_post` refuses any
host but `HBSP_HOST`, and `tests/test_canvas_read_only.py` allows POST calls in
this file alone. Canvas and Gradescope stay read-only.

**Why exceptions are raised, not caught.** A missing form or a non-PDF answer
(an item HBS offers only in its online reader, or a changed page) is a
`MitsyncError` naming the item. `sync` records it per link and continues.
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import httpx

from mitsync.canvas.client import CanvasClient
from mitsync.core.errors import MitsyncError

__all__ = ["HBSP_HOST", "download_case"]

HBSP_HOST = "services.hbsp.harvard.edu"


class _Forms(HTMLParser):
    """Every `<form>` on a page: its id, action and named input values."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self.forms.append({"id": a.get("id"), "action": a.get("action") or "", "data": {}})
        elif tag == "input" and self.forms and a.get("name"):
            self.forms[-1]["data"][a["name"]] = a.get("value") or ""


def _form(html: str, title: str, *, form_id: str | None = None, host: str | None = None) -> dict:
    parser = _Forms()
    parser.feed(html)
    for form in parser.forms:
        if (form_id is None or form["id"] == form_id) and (
            host is None or urlparse(form["action"]).netloc == host
        ):
            return form
    raise MitsyncError(f"HBS case '{title}': expected form {form_id or host} not found on the page")


def _post(web: httpx.Client, form: dict) -> httpx.Response:
    if urlparse(form["action"]).netloc != HBSP_HOST:
        raise MitsyncError(f"refusing to POST anywhere but {HBSP_HOST}: {form['action']}")
    response = web.post(form["action"], data=form["data"])
    response.raise_for_status()
    return response


def download_case(client: CanvasClient, item: dict, dest: Path) -> str:
    """Write the PDF behind one HBS module link to ``dest``; return its sha256."""
    title = str(item.get("title"))
    api = urlparse(item["url"])
    launch = client.get(api.path.removeprefix("/api/v1"), **dict(parse_qsl(api.query)))["url"]
    with httpx.Client(transport=client.transport, follow_redirects=True, timeout=120) as web:
        page = web.get(launch)
        page.raise_for_status()
        item_page = _post(web, _form(page.text, title, host=HBSP_HOST))
        pdf = _post(web, _form(item_page.text, title, form_id="pdfLaunch"))
    if not pdf.content.startswith(b"%PDF-"):
        raise MitsyncError(f"HBS case '{title}': the download was not a PDF")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(pdf.content)
    return hashlib.sha256(pdf.content).hexdigest()

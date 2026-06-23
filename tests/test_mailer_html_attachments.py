# tests/test_mailer_html_attachments.py
"""mailer.send_email gains optional HTML alternative + attachments, without
breaking the existing text-only callers. The SMTP seam is monkeypatched."""
import smtplib
from email.message import EmailMessage

import app.services.mailer as mailer
from app.config import settings


class _FakeSMTP:
    sent: EmailMessage | None = None

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self): pass
    def login(self, *a): pass
    def send_message(self, msg): _FakeSMTP.sent = msg


def _patch(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test", raising=False)
    monkeypatch.setattr(settings, "smtp_user", "u@test", raising=False)
    monkeypatch.setattr(settings, "smtp_password", "pw", raising=False)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.sent = None


def test_text_only_still_works(monkeypatch):
    _patch(monkeypatch)
    mailer.send_email("Subj", "plain body", to=["a@x.com"])
    msg = _FakeSMTP.sent
    assert msg["To"] == "a@x.com"
    assert "plain body" in msg.get_content()


def test_html_and_attachment(monkeypatch):
    _patch(monkeypatch)
    mailer.send_email(
        "Subj", "plain fallback", to=["a@x.com", "b@x.com"],
        html="<p>hi</p>",
        attachments=[("inv.xlsx", b"PK\x03\x04stub", "xlsx")],
    )
    msg = _FakeSMTP.sent
    assert msg["To"] == "a@x.com, b@x.com"
    html_parts = [p for p in msg.walk() if p.get_content_type() == "text/html"]
    assert html_parts and "<p>hi</p>" in html_parts[0].get_content()
    atts = [p for p in msg.walk() if p.get_content_disposition() == "attachment"]
    assert len(atts) == 1
    assert atts[0].get_filename() == "inv.xlsx"

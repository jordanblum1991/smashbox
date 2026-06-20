"""The stdlib-smtplib mailer seam. No real network — smtplib.SMTP is mocked."""
import smtplib

import app.services.mailer as mailer
from app.config import settings


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"], msg["Subject"], msg["From"]))


def test_send_email_uses_smtp(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "smtp_port", 587, raising=False)
    monkeypatch.setattr(settings, "smtp_user", "u@x.com", raising=False)
    monkeypatch.setattr(settings, "smtp_password", "pw", raising=False)
    monkeypatch.setattr(settings, "sync_alert_from", "", raising=False)

    mailer.send_email("Hi", "body here", to=["a@x.com", "b@x.com"])

    smtp = _FakeSMTP.instances[-1]
    assert smtp.host == "smtp.example.com" and smtp.port == 587
    assert "starttls" in smtp.calls
    assert ("login", "u@x.com") in smtp.calls
    sent = [c for c in smtp.calls if c[0] == "send"][0]
    assert sent[1] == "a@x.com, b@x.com"        # To
    assert sent[2] == "Hi"                         # Subject
    assert sent[3] == "u@x.com"                     # From falls back to smtp_user

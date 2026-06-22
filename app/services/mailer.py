"""Outbound email via stdlib smtplib — the single send seam for sync-failure
alerts. No third-party dependency. SMTP config comes from app.config; tests
monkeypatch smtplib.SMTP. Raises on send failure (the caller decides what to do).
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings


def send_email(subject: str, body: str, *, to: list[str]) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.sync_alert_from or settings.smtp_user
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)

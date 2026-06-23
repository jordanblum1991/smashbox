"""Outbound email via stdlib smtplib — the single send seam for sync-failure
alerts and the inventory report. No third-party dependency. SMTP config comes
from app.config; tests monkeypatch smtplib.SMTP. Raises on send failure (the
caller decides what to do)."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings


def send_email(
    subject: str,
    body: str,
    *,
    to: list[str],
    html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Send an email. `body` is the plain-text content; when `html` is given the
    message becomes multipart/alternative (text + html). `attachments` is a list
    of (filename, payload_bytes, mime_subtype) — e.g. ("inv.xlsx", b"...",
    "xlsx") attaches as application/<subtype>."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.sync_alert_from or settings.smtp_user
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    for filename, payload, subtype in attachments or []:
        msg.add_attachment(
            payload, maintype="application", subtype=subtype, filename=filename
        )
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)

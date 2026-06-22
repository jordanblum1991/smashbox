"""SMTP/alert settings: recipient parsing + the enabled gate."""
from app.config import Settings


def test_sync_alert_to_list_parses_comma_and_falls_back():
    s = Settings(sync_alert_to="a@x.com, b@x.com")
    assert s.sync_alert_to_list == ["a@x.com", "b@x.com"]
    s2 = Settings(sync_alert_to="", initial_admin_email="admin@x.com")
    assert s2.sync_alert_to_list == ["admin@x.com"]
    s3 = Settings(sync_alert_to="", initial_admin_email="")
    assert s3.sync_alert_to_list == []


def test_sync_alerts_enabled_requires_full_smtp_config():
    off = Settings()
    assert off.sync_alerts_enabled is False
    on = Settings(smtp_host="smtp.gmail.com", smtp_user="u@x.com",
                  smtp_password="pw", sync_alert_to="a@x.com")
    assert on.sync_alerts_enabled is True
    partial = Settings(smtp_host="smtp.gmail.com", smtp_user="u@x.com",
                       sync_alert_to="a@x.com")
    assert partial.sync_alerts_enabled is False

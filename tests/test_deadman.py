"""Dead-man's-switch: scheduler heartbeat freshness + the /status/sync endpoint."""
from datetime import timedelta

import app.services.scheduler as sched
from app.config import settings


def test_heartbeat_disabled_when_scheduler_off(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", False, raising=False)
    assert sched.heartbeat_status()["status"] == "disabled"


def test_heartbeat_ok_after_record(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True, raising=False)
    sched.record_heartbeat()
    s = sched.heartbeat_status()
    assert s["status"] == "ok"
    assert s["heartbeat_age_s"] < 60


def test_heartbeat_stale_past_threshold(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True, raising=False)
    sched.record_heartbeat()
    later = sched._heartbeat + timedelta(seconds=sched.HEARTBEAT_STALE_S + 60)
    s = sched.heartbeat_status(now=later)
    assert s["status"] == "stale"
    assert s["heartbeat_age_s"] >= sched.HEARTBEAT_STALE_S


def test_heartbeat_none_is_stale_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True, raising=False)
    monkeypatch.setattr(sched, "_heartbeat", None, raising=False)
    s = sched.heartbeat_status()
    assert s["status"] == "stale"
    assert s["heartbeat_age_s"] is None

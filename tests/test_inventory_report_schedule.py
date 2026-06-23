"""Shop inventory-report-email schedule fields + the recipients helper, and
(Task 6) the scheduler job registration."""
from app.db import Base, SessionLocal, engine
from app.models.shop import Shop
import pytest


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_recipients_list_parses_and_trims():
    s = Shop(slug="x", name="X",
             inventory_report_recipients=" a@x.com , b@x.com ,, ")
    assert s.report_recipients_list == ["a@x.com", "b@x.com"]


def test_recipients_list_empty():
    s = Shop(slug="x", name="X", inventory_report_recipients="")
    assert s.report_recipients_list == []


def test_schedule_defaults():
    with SessionLocal() as db:
        s = Shop(slug="d", name="D")
        db.add(s); db.commit(); db.refresh(s)
        assert s.inventory_report_enabled is False
        assert s.inventory_report_days == "mon"
        assert s.inventory_report_hour == 8
        assert s.inventory_report_minute == 0
        assert s.inventory_report_recipients == ""


import app.services.scheduler as sched


class _FakeScheduler:
    def __init__(self): self.jobs = {}
    def add_job(self, func, trigger=None, id=None, **k): self.jobs[id] = trigger
    def get_job(self, jid): return self.jobs.get(jid)
    def remove_job(self, jid): self.jobs.pop(jid, None)


def test_apply_report_schedule_registers_when_enabled_with_recipients(monkeypatch):
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    shop = Shop(slug="s", name="S", timezone="America/Los_Angeles",
                inventory_report_enabled=True, inventory_report_days="mon,thu",
                inventory_report_hour=8, inventory_report_minute=0,
                inventory_report_recipients="a@x.com")
    sched.apply_inventory_report_schedule(shop)
    assert sched.REPORT_JOB_ID in fake.jobs


def test_apply_report_schedule_skips_without_recipients(monkeypatch):
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    shop = Shop(slug="s", name="S", timezone="America/Los_Angeles",
                inventory_report_enabled=True, inventory_report_days="mon",
                inventory_report_recipients="")
    sched.apply_inventory_report_schedule(shop)
    assert sched.REPORT_JOB_ID not in fake.jobs


def test_apply_report_schedule_removes_when_disabled(monkeypatch):
    fake = _FakeScheduler()
    fake.jobs[sched.REPORT_JOB_ID] = object()
    monkeypatch.setattr(sched, "_scheduler", fake)
    shop = Shop(slug="s", name="S", timezone="America/Los_Angeles",
                inventory_report_enabled=False,
                inventory_report_recipients="a@x.com")
    sched.apply_inventory_report_schedule(shop)
    assert sched.REPORT_JOB_ID not in fake.jobs

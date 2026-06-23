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

"""Dashboard route — CUSTOM date range error handling.

Before the dashboard.py fix landed, the / route signature didn't accept
start_date/end_date, so a CUSTOM-mode submission from the period selector
raised ValueError("CUSTOM period requires both start_date and end_date") in
compute_pnl_view and returned 500 to the browser. The fix mirrors
/reports/pnl: parse + validate the dates and redirect with an error flash.

These tests use the direct-call pattern from tests/test_pnl_custom_range.py:
the handler is invoked directly with request=None and a real DB session.
That pattern only works for the redirect paths (which return RedirectResponse
before any TemplateResponse construction); the happy path (valid CUSTOM
dates) is covered separately by the TestClient smoke test in
tests/test_app_smoke.py — see the new "/" CUSTOM entry there.

Location-header assertions use unquote_plus (matching tests/test_ad_credit_upsert.py)
so the test asserts the human-readable error string rather than coupling to
how urlencode happens to serialize spaces.
"""
from urllib.parse import unquote_plus

from app.db import Base, SessionLocal, engine
from app.reports.pnl import PeriodKind
from app.routers.dashboard import home


def _fresh_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_dashboard_custom_missing_dates_redirects_with_error() -> None:
    _fresh_db()
    with SessionLocal() as db:
        resp = home(
            request=None,
            period=PeriodKind.CUSTOM,
            start_date=None,
            end_date=None,
            db=db,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/?period=month")
    decoded = unquote_plus(resp.headers["location"])
    assert "Custom date range requires both start and end dates" in decoded


def test_dashboard_custom_inverted_dates_redirects_with_error() -> None:
    _fresh_db()
    with SessionLocal() as db:
        resp = home(
            request=None,
            period=PeriodKind.CUSTOM,
            start_date="2026-05-28",
            end_date="2026-04-27",
            db=db,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/?period=month")
    decoded = unquote_plus(resp.headers["location"])
    assert "Start date must be on or before end date" in decoded


def test_dashboard_custom_garbage_date_redirects_with_error() -> None:
    _fresh_db()
    with SessionLocal() as db:
        resp = home(
            request=None,
            period=PeriodKind.CUSTOM,
            start_date="not-a-date",
            end_date="2026-05-28",
            db=db,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/?period=month")
    decoded = unquote_plus(resp.headers["location"])
    assert "Invalid date format" in decoded

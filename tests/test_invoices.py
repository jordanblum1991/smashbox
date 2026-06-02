"""Tests for /admin/invoices CRUD + PDF generation.

Pattern: direct-call route handlers with mocked-arg form values, same as
tests/test_skus_admin.py. Some tests use TestClient where the full FastAPI
stack matters (auth middleware + template rendering for the smoke check).

The PDF download test is dynamically skipped if WeasyPrint isn't
installed locally — batch 5 adds it to requirements.txt and the Dockerfile
system libs, after which the test runs.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, unquote_plus, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.invoice import Invoice
from app.routers.invoices import (
    _suggest_next_number,
    invoice_create,
    invoice_detail,
    invoice_mark_paid,
    invoice_new_form,
    invoice_preview,
    invoices_list,
)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _seed(db, number: str, **overrides) -> Invoice:
    """Insert an invoice with sensible defaults; override per test."""
    defaults = dict(
        number=number,
        issue_date=date(2026, 5, 29),
        bill_to_block="Smashbox Beauty Cosmetics\n7 Corporate Center Drive\nMelville, NY 11747",
        description_headline="TikTok Shop Advertising Spend — May 2026",
        description_subtitle=None,
        period_label=None,
        amount=Decimal("100.00"),
        status="issued",
        brand_code="SMASHBOX",
    )
    defaults.update(overrides)
    inv = Invoice(**defaults)
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _create_form_payload(**overrides) -> dict:
    """Form payload with sensible defaults; override per test."""
    payload = dict(
        number="OL-2026-007",
        issue_date="2026-05-29",
        description_preset="ad_spend",
        description_headline="TikTok Shop Advertising Spend — May 2026",
        description_subtitle="Smashbox-funded portion of seller discounts.",
        period_label="Data period: April 27 – May 28, 2026",
        bill_to_block="Smashbox Beauty Cosmetics\n7 Corporate Center Drive\nMelville, NY 11747",
        amount="8710.33",
    )
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. Number suggestion logic
# ---------------------------------------------------------------------------

def test_suggest_next_number_no_existing_invoices():
    """With no past invoices, default to OL-2026-007 (per user — OL-2026-005
    and -006 were issued externally before this feature shipped)."""
    with SessionLocal() as db:
        assert _suggest_next_number(db) == "OL-2026-007"


def test_suggest_next_number_max_plus_one():
    """OL-2026-005 + -006 in DB → suggest -007."""
    with SessionLocal() as db:
        _seed(db, "OL-2026-005")
        _seed(db, "OL-2026-006")
        assert _suggest_next_number(db) == "OL-2026-007"


def test_suggest_next_number_with_gap_uses_max_not_gap_fill():
    """OL-2026-007 + -010 in DB → suggest -011 (max+1), not -008 (gap-fill)."""
    with SessionLocal() as db:
        _seed(db, "OL-2026-007")
        _seed(db, "OL-2026-010")
        assert _suggest_next_number(db) == "OL-2026-011"


# ---------------------------------------------------------------------------
# 2. Create — happy path
# ---------------------------------------------------------------------------

def test_create_invoice_happy_path():
    """POST /admin/invoices with valid form → 303 to /admin/invoices/{id},
    Invoice row exists with correct field values."""
    with SessionLocal() as db:
        resp = invoice_create(request=None, db=db, **_create_form_payload())

    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/admin/invoices/")
    # Extract id from path "/admin/invoices/{id}?notice=..."
    inv_id = int(urlparse(loc).path.rsplit("/", 1)[-1])

    with SessionLocal() as db:
        inv = db.get(Invoice, inv_id)
    assert inv is not None
    assert inv.number == "OL-2026-007"
    assert inv.issue_date == date(2026, 5, 29)
    assert inv.description_headline == "TikTok Shop Advertising Spend — May 2026"
    assert inv.description_subtitle == "Smashbox-funded portion of seller discounts."
    assert inv.period_label == "Data period: April 27 – May 28, 2026"
    assert inv.bill_to_block.startswith("Smashbox Beauty Cosmetics")
    assert inv.amount == Decimal("8710.33")
    assert inv.status == "issued"
    assert inv.brand_code == "SMASHBOX"


# ---------------------------------------------------------------------------
# 3. Validation — each rule produces a redirect with error + no DB row
# ---------------------------------------------------------------------------

def _assert_error_redirect(resp, expected_phrase: str) -> None:
    """Helper: assert the response is a 303 to /admin/invoices/new with the
    expected error phrase in the (URL-decoded) query string."""
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/invoices/new")
    decoded = unquote_plus(resp.headers["location"])
    assert expected_phrase in decoded, (
        f"expected {expected_phrase!r} in location, got {decoded!r}"
    )


def test_create_rejects_amount_zero():
    with SessionLocal() as db:
        resp = invoice_create(request=None, db=db, **_create_form_payload(amount="0"))
        _assert_error_redirect(resp, "Amount must be greater than $0.00")
        assert db.query(Invoice).count() == 0


def test_create_rejects_amount_negative():
    with SessionLocal() as db:
        resp = invoice_create(request=None, db=db, **_create_form_payload(amount="-50"))
        _assert_error_redirect(resp, "Amount must be greater than $0.00")
        assert db.query(Invoice).count() == 0


def test_create_rejects_garbage_amount():
    with SessionLocal() as db:
        resp = invoice_create(request=None, db=db, **_create_form_payload(amount="abc"))
        _assert_error_redirect(resp, "'abc' is not a valid number")
        assert db.query(Invoice).count() == 0


def test_create_rejects_missing_issue_date():
    with SessionLocal() as db:
        resp = invoice_create(request=None, db=db, **_create_form_payload(issue_date=""))
        _assert_error_redirect(resp, "Issue date is required")
        assert db.query(Invoice).count() == 0


def test_create_rejects_blank_bill_to():
    with SessionLocal() as db:
        resp = invoice_create(request=None, db=db, **_create_form_payload(bill_to_block="   "))
        _assert_error_redirect(resp, "Bill To block is required")
        assert db.query(Invoice).count() == 0


def test_create_rejects_blank_description_headline():
    with SessionLocal() as db:
        resp = invoice_create(request=None, db=db, **_create_form_payload(description_headline=" "))
        _assert_error_redirect(resp, "Description headline is required")
        assert db.query(Invoice).count() == 0


def test_create_rejects_blank_number():
    with SessionLocal() as db:
        resp = invoice_create(request=None, db=db, **_create_form_payload(number="  "))
        _assert_error_redirect(resp, "Invoice number is required")
        assert db.query(Invoice).count() == 0


# ---------------------------------------------------------------------------
# 4. Duplicate number — form re-render with error, no second row
# ---------------------------------------------------------------------------

def test_create_rejects_duplicate_number():
    with SessionLocal() as db:
        _seed(db, "OL-2026-007")

    with SessionLocal() as db:
        resp = invoice_create(request=None, db=db, **_create_form_payload(number="OL-2026-007"))
        _assert_error_redirect(resp, "'OL-2026-007' is already in use")
        # Only the seeded one — no second row.
        assert db.query(Invoice).count() == 1


# ---------------------------------------------------------------------------
# 5. Mark paid
# ---------------------------------------------------------------------------

def test_mark_paid_flips_status():
    """POST /admin/invoices/{id}/mark-paid on an 'issued' invoice → status
    becomes 'paid', 303 redirect back to detail."""
    with SessionLocal() as db:
        inv = _seed(db, "OL-2026-007")
        assert inv.status == "issued"

    with SessionLocal() as db:
        resp = invoice_mark_paid(invoice_id=inv.id, request=None, db=db)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/admin/invoices/{inv.id}")
    with SessionLocal() as db:
        assert db.get(Invoice, inv.id).status == "paid"


def test_mark_paid_idempotent_when_already_paid():
    """A second mark-paid on a paid invoice is a no-op — no error, status
    stays 'paid', notice reflects 'already paid'."""
    with SessionLocal() as db:
        inv = _seed(db, "OL-2026-007", status="paid")

    with SessionLocal() as db:
        resp = invoice_mark_paid(invoice_id=inv.id, request=None, db=db)

    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "is already paid" in decoded
    with SessionLocal() as db:
        assert db.get(Invoice, inv.id).status == "paid"


def test_mark_paid_404_for_missing_invoice():
    with SessionLocal() as db:
        resp = invoice_mark_paid(invoice_id=99999, request=None, db=db)
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "Invoice not found" in decoded
    assert resp.headers["location"].startswith("/admin/invoices?")


# ---------------------------------------------------------------------------
# 6. List + detail + preview views render
# ---------------------------------------------------------------------------

# These three tests use the TestClient because the template rendering needs
# a real Request. Auth is disabled in the test environment (empty
# session_secret), so admin routes return 200 directly.

@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_list_view_renders_with_invoices(client: TestClient):
    """GET /admin/invoices renders 200, shows the seeded invoice with its
    number, amount, and status pill."""
    with SessionLocal() as db:
        _seed(db, "OL-2026-007", amount=Decimal("8710.33"))
        _seed(db, "OL-2026-008", amount=Decimal("779.54"), status="paid")

    r = client.get("/admin/invoices")
    assert r.status_code == 200
    assert "OL-2026-007" in r.text
    assert "OL-2026-008" in r.text
    assert "$8,710.33" in r.text
    assert "$779.54" in r.text
    # Status pills present
    assert "Issued" in r.text
    assert "Paid" in r.text


def test_list_view_empty_state(client: TestClient):
    r = client.get("/admin/invoices")
    assert r.status_code == 200
    assert "No invoices yet" in r.text


def test_detail_view_renders(client: TestClient):
    with SessionLocal() as db:
        inv = _seed(
            db, "OL-2026-007",
            amount=Decimal("8710.33"),
            description_headline="TikTok Shop Advertising Spend — May 2026",
        )

    r = client.get(f"/admin/invoices/{inv.id}")
    assert r.status_code == 200
    # Page title, status, amount all visible on detail.
    assert "OL-2026-007" in r.text
    assert "$8,710.33" in r.text
    # The iframe src points at /preview.
    assert f"/admin/invoices/{inv.id}/preview" in r.text


def test_preview_renders_bare_invoice(client: TestClient):
    """GET /admin/invoices/{id}/preview returns the bare invoice document
    (no app chrome / nav). The Outlandish wordmark and the headline are
    present; the navbar is NOT."""
    with SessionLocal() as db:
        inv = _seed(
            db, "OL-2026-007",
            description_headline="TikTok Shop Advertising Spend — May 2026",
            description_subtitle="Smashbox-funded portion of seller discounts.",
            period_label="Data period: April 27 – May 28, 2026",
        )

    r = client.get(f"/admin/invoices/{inv.id}/preview")
    assert r.status_code == 200
    # Invoice document content
    assert "Outlandish" in r.text
    assert "INVOICE" in r.text
    assert "BILL TO" in r.text
    assert "TikTok Shop Advertising Spend — May 2026" in r.text
    assert "Smashbox-funded portion of seller discounts." in r.text
    assert "Data period: April 27" in r.text
    assert "TOTAL DUE" in r.text
    # No app chrome (the base nav has the "Smashbox" badge + "TikTok P&L"
    # which appears in base.html but not in invoice_pdf.html).
    assert "TikTok P&L" not in r.text


# ---------------------------------------------------------------------------
# 7. PDF download — skipped if WeasyPrint isn't installed (batch 5)
# ---------------------------------------------------------------------------

def test_pdf_download_content_type_and_disposition(client: TestClient):
    """GET /admin/invoices/{id}/pdf returns application/pdf with the
    correct Content-Disposition header. Skipped until WeasyPrint is in
    requirements.txt (batch 5)."""
    try:
        import weasyprint  # noqa: F401
    except ImportError:
        pytest.skip("WeasyPrint not installed yet — batch 5 will add it")

    with SessionLocal() as db:
        inv = _seed(db, "OL-2026-007", amount=Decimal("8710.33"))

    r = client.get(f"/admin/invoices/{inv.id}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'attachment; filename="OL-2026-007.pdf"'
    # PDFs start with %PDF-
    assert r.content[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# 8. Auth gating — unauthenticated → 303 to /login
# ---------------------------------------------------------------------------

def test_unauthenticated_admin_invoices_blocked(monkeypatch, client: TestClient):
    """Unauthenticated requests to every /admin/invoices/* route must be
    blocked. The exact response depends on which middleware is active:

      - In production, SessionAuthMiddleware redirects to /login (303).
      - In tests, that middleware is conditionally NOT registered (app/main.py
        gates `app.add_middleware(SessionAuthMiddleware)` on
        `settings.session_secret` being non-empty AT APP CONSTRUCTION TIME).
        Monkey-patching the setting mid-test can't retroactively add a
        middleware, so the request reaches `require_admin` directly, which
        raises 403.

    Both are valid security responses — the assertion accepts either, with
    a stronger assertion on /login when the response IS a redirect."""
    monkeypatch.setattr(settings, "session_secret", "test-secret-for-auth-test")

    paths = (
        "/admin/invoices",
        "/admin/invoices/new",
        "/admin/invoices/1",
        "/admin/invoices/1/preview",
        "/admin/invoices/1/pdf",
    )
    for path in paths:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (303, 403), (
            f"{path}: expected 303 or 403, got {r.status_code}"
        )
        if r.status_code == 303:
            assert "/login" in r.headers["location"], (
                f"{path}: {r.headers['location']!r}"
            )

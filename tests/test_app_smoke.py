"""Smoke test: the app boots, pages render, no template errors."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        # Regression guard for the CUSTOM-range 500 fixed in
        # hotfix/dashboard-custom-range-500: dashboard now parses + validates
        # start_date / end_date instead of letting compute_pnl_view raise.
        "/?period=custom&start_date=2026-04-27&end_date=2026-05-28",
        "/uploads",
        "/reports/pnl",
        "/reports/pnl?period=year&year=2026",
        "/reports/pnl?period=ytd&year=2026&month=4",
        "/reports/monthly-pnl",
        "/reports/ytd-pnl",
        "/reports/sku-profitability",
        "/reports/unmapped-skus",
        "/reports/settlement-only-orders",
        "/reports/samples",
        "/reports/policy-violations",
        "/reports/recon-health?tab=recon",
        # Admin-gated route — auth is disabled in tests
        # (settings.session_secret == ""), so the smoke test can hit it
        # like any other route.
        "/admin/invoices",
    ],
)
def test_page_renders(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:300]}"
    assert "Smashbox" in r.text


def test_excel_export(client: TestClient) -> None:
    r = client.get("/export/monthly-pnl.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert len(r.content) > 0


def test_csv_export(client: TestClient) -> None:
    r = client.get("/export/sku-profitability.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "tiktok_sku_id,sku_code,name" in r.text

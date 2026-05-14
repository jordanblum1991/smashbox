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
        "/uploads",
        "/reports/monthly-pnl",
        "/reports/ytd-pnl",
        "/reports/sku-profitability",
        "/reports/samples",
        "/reports/reconciliation",
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

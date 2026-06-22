"""Mobile responsiveness guards. These assert structural markers (responsive
classes / mobile-menu markup) survive future edits. They do NOT validate visual
layout — the acceptance test is a human eyeball on a phone."""
import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def test_main_container_uses_responsive_padding(client):
    html = client.get("/").text
    # Mobile gets tighter px-4; sm+ restores px-6.
    assert "px-4 sm:px-6" in html


def test_nav_has_desktop_bar_and_mobile_menu(client):
    html = client.get("/").text
    assert "hidden md:flex" in html          # desktop links gated to md+
    assert "md:hidden" in html               # mobile hamburger
    assert 'id="mobile-menu"' in html
    assert html.count('href="/reports/pnl"') >= 2    # desktop bar + mobile menu
    assert html.count('href="/reports/sales"') >= 2


def test_nav_mobile_menu_has_grouped_sections(client):
    html = client.get("/").text
    for label in ("Samples", "Ads", "Inventory"):
        assert label in html


def test_pnl_percent_column_hidden_on_mobile(client):
    r = client.get("/reports/pnl")
    assert r.status_code == 200
    # The % column cells/header are gated to sm+ so mobile shows label + amount only.
    assert "hidden sm:table-cell" in r.text


@pytest.mark.parametrize("url", ["/reports/ad-spend", "/reports/sales", "/"])
def test_target_pages_render_after_mobile_pass(client, url):
    assert client.get(url).status_code == 200


def test_ad_spend_uses_responsive_padding(client):
    # Ad Spend's heavy px-6 sections get a mobile-tighter variant.
    assert "px-4 sm:px-6" in client.get("/reports/ad-spend").text

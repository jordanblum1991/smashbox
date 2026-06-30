"""Inventory Report page renders + carries the sellable/sample view toggle."""
import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_inventory_report_renders_with_view_toggle():
    r = TestClient(app).get("/reports/inventory")
    assert r.status_code == 200
    # the segmented control + the column hooks the JS toggles
    assert 'data-mode="all"' in r.text
    assert 'data-mode="sellable"' in r.text
    assert 'data-mode="sample"' in r.text
    assert "col-sellable" in r.text and "col-sample" in r.text


def test_inventory_report_has_hide_zero_checkbox_and_sellable_onorder_label():
    r = TestClient(app).get("/reports/inventory")
    assert r.status_code == 200
    assert 'id="inv-hidezero"' in r.text          # hide zero-stock checkbox
    assert "On order (sellable)" in r.text        # clarified column label

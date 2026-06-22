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

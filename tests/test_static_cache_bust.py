"""The compiled CSS link must carry a cache-busting ?v= token so a deploy that
adds new utility classes can't strand browsers on a stale cached stylesheet."""
from fastapi.testclient import TestClient

from app.main import app


def test_login_css_link_is_cache_busted():
    r = TestClient(app).get("/login")
    assert r.status_code == 200
    assert "tailwind.css?v=" in r.text

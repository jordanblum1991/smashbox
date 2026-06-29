"""The analytics sync must re-pull a trailing window each run.

TikTok publishes provisional daily shop-performance metrics (often GMV=0) and
finalizes them a few days later. A watermark-only pull fetches each day ONCE
(start = synced_through ≈ yesterday) and never corrects it, so finalized values
never overwrite the provisional 0s — the days freeze at $0 (observed on prod for
Jun 15/16/19/22-28 2026, surfacing as false amber on the reconciliation page).

fetch_analytics' own docstring promises a trailing re-pull; this guards that the
implementation actually reaches back far enough, regardless of how recent the
watermark is.
"""
from datetime import date, datetime, timedelta

import pytest

from app.db import Base, SessionLocal, engine
from app.models.tiktok_credential import TikTokCredential
from app.services import tiktok_api, tiktok_fetchers


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_fetch_analytics_repulls_trailing_window_despite_recent_watermark(monkeypatch):
    captured = {}

    def fake_perf(cred, start, end):
        captured["start"] = start
        captured["end"] = end
        return []  # no rows -> fetch returns early, but the call args are recorded

    monkeypatch.setattr(tiktok_api, "get_shop_performance", fake_perf)
    cred = TikTokCredential(access_token="a", refresh_token="r", shop_cipher="C",
                            access_expires_at=datetime(2030, 1, 1))

    with SessionLocal() as db:
        # Watermark from "yesterday" — the everyday incremental case.
        recent_watermark = datetime.combine(date.today() - timedelta(days=1), datetime.min.time())
        tiktok_fetchers.fetch_analytics(db, cred, recent_watermark)

    start = date.fromisoformat(captured["start"])
    # Despite the 1-day-old watermark, the pull must reach well back so late-
    # finalized provisional days get overwritten. (Bug: start == yesterday.)
    assert start <= date.today() - timedelta(days=10), (
        f"analytics start {start} did not re-pull a trailing window "
        f"(watermark was yesterday)"
    )

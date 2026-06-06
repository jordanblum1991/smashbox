"""Uploads must not block the event loop.

The importer runs synchronously (pandas parse + upserts). If it executes
directly inside the `async def upload_file` handler, it freezes uvicorn's
single event loop and the WHOLE app goes unresponsive for the import's
duration — this caused a 16-minute prod outage (a settlement import that ran
long on a cold machine). Running it via `run_in_threadpool` keeps the loop
free to serve every other request while the import proceeds.
"""
from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.importers import IMPORTERS
from app.importers.base import BaseImporter, ImportResult
from app.main import app
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind

SLEEP = 2.0  # simulated import duration
_started = threading.Event()  # set the instant the import begins running


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


class _SlowImporter(BaseImporter):
    """Stands in for a long-running import (workbook parse + upserts)."""

    def run(self, path, db, batch) -> ImportResult:
        _started.set()
        time.sleep(SLEEP)
        return ImportResult(rows_imported=1)


class _FastImporter(BaseImporter):
    def run(self, path, db, batch) -> ImportResult:
        return ImportResult(rows_imported=7)


def _upload(ac):
    return ac.post(
        "/uploads",
        data={"kind": ImportFileKind.TIKTOK_ADS.value},
        files={"file": ("ads.csv", b"col\n1", "text/csv")},
    )


def test_upload_does_not_block_other_requests(monkeypatch, tmp_path):
    _started.clear()
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    monkeypatch.setitem(IMPORTERS, ImportFileKind.TIKTOK_ADS, _SlowImporter)

    async def scenario():
        loop = asyncio.get_event_loop()
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            await ac.get("/login")  # warm up template compile so timing is clean

            t0 = loop.time()
            up = asyncio.create_task(_upload(ac))
            # Block OFF the event loop until the import has actually begun, so we
            # measure responsiveness DURING the import — not before it starts.
            # (If the loop is blocked, this resume itself is delayed by ~SLEEP.)
            await loop.run_in_executor(None, _started.wait, 5.0)

            r = await ac.get("/login")
            elapsed = loop.time() - t0
            await up
            return r.status_code, elapsed

    status, elapsed = asyncio.run(scenario())
    assert status == 200
    # Served while the import is still running, not queued behind it. Blocking
    # the loop would push this out to ~SLEEP.
    assert elapsed < SLEEP * 0.5, (
        f"concurrent request resolved at {elapsed:.2f}s — the event loop was "
        f"blocked by the {SLEEP}s import"
    )


def test_upload_still_imports_after_refactor(monkeypatch, tmp_path):
    """Regression: the upload path still runs the importer and records a
    COMPLETED batch with the importer's row count."""
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    monkeypatch.setitem(IMPORTERS, ImportFileKind.TIKTOK_ADS, _FastImporter)

    async def scenario():
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            return await _upload(ac)

    resp = asyncio.run(scenario())
    assert resp.status_code in (303, 200)
    with SessionLocal() as db:
        batch = db.query(ImportBatch).order_by(ImportBatch.id.desc()).first()
        assert batch is not None
        assert batch.status == ImportBatchStatus.COMPLETED
        assert batch.rows_imported == 7

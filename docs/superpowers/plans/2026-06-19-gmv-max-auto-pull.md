# GMV-Max Auto-Pull Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual GMV-Max "Campaign overview By-Day" CSV upload with an automatic pull from TikTok's Marketing API into `GmvMaxDailyMetric`, triggered by a manual button and the existing weekday SAP scheduler job.

**Architecture:** A new `gmv_max_sync.py` service mirrors `inventory_sync.py`: it discovers the advertiser's GMV-Max campaigns + their store ids, pulls `/gmv_max/report/get/` in ≤30-day chunks, aggregates campaign×day rows into by-day totals, and writes one `ImportBatch(kind=TIKTOK_GMV_MAX)` through a shared `import_dataframe` seam on the existing importer (idempotent upsert by `metric_date`). The Marketing-API HTTP lives in `tiktok_marketing_api.py` behind a single `_api_get` seam so tests never hit the network.

**Tech Stack:** FastAPI/Starlette, SQLAlchemy 2.x, pandas, httpx, APScheduler, pytest. Spec: `docs/superpowers/specs/2026-06-19-gmv-max-auto-pull-design.md`.

**Proven API facts (verified on prod 2026-06-19):**
- List GMV-Max campaigns: `GET /gmv_max/campaign/get/` with `filtering={"gmv_max_promotion_types":["PRODUCT_GMV_MAX","LIVE_GMV_MAX"]}`. Rows carry `campaign_id`/`campaign_name`/status — **no** store id.
- Store id: `GET /campaign/gmv_max/info/` with `advertiser_id`+`campaign_id` → top-level `store_id` (e.g. `"7494362432882967723"`).
- Report: `GET /gmv_max/report/get/` with `advertiser_id`, `store_ids=[...]` (**required**), `dimensions=["campaign_id","stat_time_day"]`, `metrics=["cost","orders","gross_revenue"]`, `start_date`/`end_date` (**≤30-day window**), `page`/`page_size`. Row: `{"dimensions":{"campaign_id","stat_time_day":"YYYY-MM-DD 00:00:00"},"metrics":{"cost","orders","gross_revenue",...}}`.
- Parity: summed over May 2026 → cost `7824.02`, gross_revenue `15769.65` (matches the uploaded CSV to the cent).

**Branch:** `feature/gmv-max-auto-pull` (already created; spec committed).

---

## File Structure

- **Modify** `app/importers/gmv_max_campaign.py` — extract a shared `import_dataframe(df, db, batch)` seam consuming normalized columns `metric_date`/`cost`/`sku_orders`/`gross_revenue`; `run()` builds that frame from the Excel and delegates.
- **Modify** `app/services/tiktok_marketing_api.py` — add `_api_get` seam + `list_gmv_max_campaigns`, `gmv_max_store_ids`, `get_gmv_max_report`.
- **Create** `app/services/gmv_max_sync.py` — `_date_chunks` + `sync_gmv_max` orchestration (never raises; records on the `ImportBatch`).
- **Modify** `app/services/scheduler.py` — the existing `_run_inventory_sync_job` also runs `sync_gmv_max` (independent try/except).
- **Modify** `app/routers/uploads.py` — `POST /uploads/sync-gmv-max` button handler + `last_gmv_sync` context.
- **Modify** `app/templates/uploads.html` — a "Live GMV-Max feed (TikTok API)" card mirroring the SAP card.
- **Create** `tests/test_gmv_max_sync.py` — unit + integration tests (chunker, aggregation/parity, idempotency, no-cred, no-campaigns).
- **Create** `tests/test_gmv_max_importer_seam.py` — the `import_dataframe` seam.
- **Modify** `tests/test_uploads_page.py` (or create `tests/test_gmv_max_button.py`) — button + card.

---

## Task 1: Extract `import_dataframe` seam on the GMV-Max importer

**Files:**
- Modify: `app/importers/gmv_max_campaign.py`
- Test: `tests/test_gmv_max_importer_seam.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gmv_max_importer_seam.py
"""The GMV-Max importer exposes a DataFrame seam (import_dataframe) shared by the
CSV run() path and the API sync. Normalized columns: metric_date / cost /
sku_orders / gross_revenue. Idempotent upsert by metric_date."""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from app.db import Base, SessionLocal, engine
from app.importers.gmv_max_campaign import import_dataframe
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.PROCESSING,
                    original_filename="api", stored_path="")
    db.add(b); db.flush()
    return b


def test_import_dataframe_inserts_rows():
    with SessionLocal() as db:
        b = _batch(db)
        df = pd.DataFrame([
            {"metric_date": date(2026, 5, 10), "cost": Decimal("100.00"),
             "sku_orders": 5, "gross_revenue": Decimal("300.00")},
            {"metric_date": date(2026, 5, 11), "cost": Decimal("50.00"),
             "sku_orders": 2, "gross_revenue": Decimal("120.00")},
        ])
        res = import_dataframe(df, db, b)
        db.commit()
        assert res.rows_imported == 2
        rows = db.query(GmvMaxDailyMetric).order_by(GmvMaxDailyMetric.metric_date).all()
        assert [(r.metric_date, r.cost, r.sku_orders, r.gross_revenue) for r in rows] == [
            (date(2026, 5, 10), Decimal("100.00"), 5, Decimal("300.00")),
            (date(2026, 5, 11), Decimal("50.00"), 2, Decimal("120.00")),
        ]


def test_import_dataframe_upserts_same_day():
    with SessionLocal() as db:
        b = _batch(db)
        import_dataframe(pd.DataFrame([
            {"metric_date": date(2026, 5, 10), "cost": Decimal("100.00"),
             "sku_orders": 5, "gross_revenue": Decimal("300.00")}]), db, b)
        db.commit()
        b2 = _batch(db)
        import_dataframe(pd.DataFrame([
            {"metric_date": date(2026, 5, 10), "cost": Decimal("111.00"),
             "sku_orders": 7, "gross_revenue": Decimal("333.00")}]), db, b2)
        db.commit()
        rows = db.query(GmvMaxDailyMetric).all()
        assert len(rows) == 1                       # upsert, not append
        assert rows[0].cost == Decimal("111.00")
        assert rows[0].sku_orders == 7
        assert rows[0].import_batch_id == b2.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_gmv_max_importer_seam.py -v 2>&1 | tail -20`
Expected: FAIL — `ImportError: cannot import name 'import_dataframe'`.

- [ ] **Step 3: Refactor the importer to add the seam**

In `app/importers/gmv_max_campaign.py`, replace the `run` method and add `import_dataframe`. Keep the existing `_str`/`_parse_date`/`_dec`/`_int` helpers and `COL` map unchanged. New code:

```python
class GmvMaxCampaignImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        df = pd.read_excel(path, dtype=str)
        missing = [c for c in (COL["date"], COL["cost"]) if c not in df.columns]
        if missing:
            raise ValueError(f"GMV Max campaign file missing required columns: {missing}")

        rows = []
        for _, row in df.iterrows():
            day = _parse_date(row.get(COL["date"]))
            if day is None:
                # Blank cells and the footer TOTAL row ("-") are skipped silently.
                continue
            rows.append({
                "metric_date": day,
                "cost": _dec(row.get(COL["cost"])),
                "sku_orders": _int(row.get(COL["sku_orders"])),
                "gross_revenue": _dec(row.get(COL["gross_revenue"])),
            })
        return import_dataframe(pd.DataFrame(rows), db, batch)


def import_dataframe(df: pd.DataFrame, db: Session, batch: ImportBatch) -> ImportResult:
    """Upsert by-day GMV-Max metrics from an already-normalized frame.

    The shared core of both ingestion paths: the CSV/XLSX importer (`run`, which
    reads a file first) and the API sync (`app/services/gmv_max_sync.py`, which
    builds the frame from `/gmv_max/report/get/`). Columns must be normalized to
    `metric_date` (date) / `cost` (Decimal) / `sku_orders` (int) /
    `gross_revenue` (Decimal). Idempotent on `metric_date`: a re-run overwrites
    each day in place, so TikTok's revisions to recent days flow through."""
    result = ImportResult()
    if df.empty:
        return result
    for _, row in df.iterrows():
        day = row["metric_date"]
        try:
            _upsert_row(db, day, row, batch)
            result.rows_imported += 1
        except Exception as exc:  # noqa: BLE001
            result.skip(f"{day}: {exc}")
    return result


def _upsert_row(db: Session, day, row, batch: ImportBatch) -> GmvMaxDailyMetric:
    payload = dict(
        import_batch_id=batch.id,
        metric_date=day,
        cost=row["cost"],
        sku_orders=int(row["sku_orders"]),
        gross_revenue=row["gross_revenue"],
    )
    existing = db.execute(
        select(GmvMaxDailyMetric).where(GmvMaxDailyMetric.metric_date == day)
    ).scalar_one_or_none()
    if existing is None:
        obj = GmvMaxDailyMetric(**payload)
        db.add(obj)
        return obj
    for k, v in payload.items():
        setattr(existing, k, v)
    return existing
```

Delete the old module-level `_upsert` function (replaced by `_upsert_row`, which takes the normalized row).

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_gmv_max_importer_seam.py -v 2>&1 | tail -20`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the existing GMV-Max CSV importer + Ad Spend tests (regression)**

Run: `py -m pytest tests/test_ad_spend_page.py -k gmv -q 2>&1 | tail -15; py -m pytest -k gmv_max -q 2>&1 | tail -15`
Expected: PASS (no regressions in the CSV path or Ad Spend page).

- [ ] **Step 6: Commit**

```bash
git add app/importers/gmv_max_campaign.py tests/test_gmv_max_importer_seam.py
git commit -F .git/COMMIT_MSG_DRAFT.txt   # message: "gmv-max: extract import_dataframe seam on the importer"
```

---

## Task 2: Marketing-API client functions (list campaigns, store ids, report)

**Files:**
- Modify: `app/services/tiktok_marketing_api.py`
- Test: `tests/test_gmv_max_api_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gmv_max_api_client.py
"""GMV-Max Marketing-API client functions. The raw HTTP is isolated behind
_api_get (one seam) so tests stub responses without httpx."""
from decimal import Decimal

import app.services.tiktok_marketing_api as api


def _stub(monkeypatch, responses):
    """responses: dict path -> list of unwrapped `data` dicts (one per page call)."""
    calls = {"log": []}
    state = {p: list(v) for p, v in responses.items()}

    def fake(path, params, access_token):
        calls["log"].append((path, dict(params)))
        return state[path].pop(0)

    monkeypatch.setattr(api, "_api_get", fake)
    return calls


def test_list_gmv_max_campaigns_paginates(monkeypatch):
    _stub(monkeypatch, {
        "/gmv_max/campaign/get/": [
            {"list": [{"campaign_id": "1"}], "page_info": {"total_page": 2}},
            {"list": [{"campaign_id": "2"}], "page_info": {"total_page": 2}},
        ],
    })
    out = api.list_gmv_max_campaigns("tok", "adv1")
    assert [c["campaign_id"] for c in out] == ["1", "2"]


def test_gmv_max_store_ids_dedup(monkeypatch):
    _stub(monkeypatch, {
        "/campaign/gmv_max/info/": [
            {"store_id": "STORE_A"},
            {"store_id": "STORE_A"},   # same store → deduped
            {"store_id": "STORE_B"},
        ],
    })
    out = api.gmv_max_store_ids("tok", "adv1",
                                [{"campaign_id": "1"}, {"campaign_id": "2"}, {"campaign_id": "3"}])
    assert out == ["STORE_A", "STORE_B"]


def test_get_gmv_max_report_parses_rows(monkeypatch):
    _stub(monkeypatch, {
        "/gmv_max/report/get/": [
            {"list": [
                {"dimensions": {"campaign_id": "1", "stat_time_day": "2026-05-10 00:00:00"},
                 "metrics": {"cost": "100.00", "orders": "5", "gross_revenue": "300.00"}},
            ], "page_info": {"total_page": 1}},
        ],
    })
    out = api.get_gmv_max_report("tok", "adv1", ["STORE_A"], "2026-05-10", "2026-05-10")
    assert out == [{"stat_day": "2026-05-10", "cost": Decimal("100.00"),
                    "orders": 5, "gross_revenue": Decimal("300.00")}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_gmv_max_api_client.py -v 2>&1 | tail -20`
Expected: FAIL — `AttributeError: module ... has no attribute '_api_get'` / `list_gmv_max_campaigns`.

- [ ] **Step 3: Add the client functions**

Append to `app/services/tiktok_marketing_api.py` (after `get_ad_spend`). `json`, `Decimal`, `BASE`, `_TIMEOUT`, `_unwrap`, `_to_decimal` already exist in the module.

```python
def _api_get(path: str, params: dict, access_token: str) -> dict:
    """Single GET seam for Marketing-API reads — returns the unwrapped `data`
    dict. Isolated so tests stub it instead of hitting the network."""
    import httpx

    r = httpx.get(
        f"{BASE}{path}", params=params,
        headers={"Access-Token": access_token}, timeout=_TIMEOUT,
    )
    return _unwrap(r)


def list_gmv_max_campaigns(access_token: str, advertiser_id: str) -> list[dict]:
    """All GMV-Max campaigns for the advertiser (paginated). Each row carries
    campaign_id / campaign_name / status — NOT a store id (see gmv_max_store_ids)."""
    out: list[dict] = []
    page = 1
    while True:
        data = _api_get("/gmv_max/campaign/get/", {
            "advertiser_id": advertiser_id,
            "filtering": json.dumps(
                {"gmv_max_promotion_types": ["PRODUCT_GMV_MAX", "LIVE_GMV_MAX"]}),
            "page": page, "page_size": 100,
        }, access_token)
        out.extend(data.get("list") or [])
        total = int((data.get("page_info") or {}).get("total_page") or 1)
        if page >= total:
            break
        page += 1
    return out


def gmv_max_store_ids(access_token: str, advertiser_id: str,
                      campaigns: list[dict]) -> list[str]:
    """Distinct TikTok-Shop store ids backing the GMV-Max campaigns. The report
    endpoint is store-keyed (returns all campaigns for a store), so we only need
    the unique set — one store today. Reads each campaign's `/campaign/gmv_max/
    info/` top-level `store_id`."""
    stores: list[str] = []
    for c in campaigns:
        cid = str(c.get("campaign_id") or "").strip()
        if not cid:
            continue
        data = _api_get("/campaign/gmv_max/info/",
                        {"advertiser_id": advertiser_id, "campaign_id": cid}, access_token)
        sid = str(data.get("store_id") or "").strip()
        if sid and sid not in stores:
            stores.append(sid)
    return stores


def get_gmv_max_report(access_token: str, advertiser_id: str, store_ids: list[str],
                       start_date: str, end_date: str) -> list[dict]:
    """Daily GMV-Max metrics per campaign for [start_date, end_date] (≤30 days,
    inclusive, YYYY-MM-DD). Returns
    [{stat_day(str), cost(Decimal), orders(int), gross_revenue(Decimal)}],
    paginated. Caller sums across campaigns/days."""
    out: list[dict] = []
    page = 1
    while True:
        data = _api_get("/gmv_max/report/get/", {
            "advertiser_id": advertiser_id,
            "store_ids": json.dumps(list(store_ids)),
            "dimensions": json.dumps(["campaign_id", "stat_time_day"]),
            "metrics": json.dumps(["cost", "orders", "gross_revenue"]),
            "start_date": start_date, "end_date": end_date,
            "page": page, "page_size": 1000,
        }, access_token)
        for item in data.get("list") or []:
            dims = item.get("dimensions") or {}
            mets = item.get("metrics") or {}
            stat = (dims.get("stat_time_day") or "")[:10]
            if not stat:
                continue
            out.append({
                "stat_day": stat,
                "cost": _to_decimal(mets.get("cost")),
                "orders": _int_metric(mets.get("orders")),
                "gross_revenue": _to_decimal(mets.get("gross_revenue")),
            })
        total = int((data.get("page_info") or {}).get("total_page") or 1)
        if page >= total:
            break
        page += 1
    return out


def _int_metric(v) -> int:
    try:
        return int(Decimal(str(v)))
    except (InvalidOperation, TypeError, ValueError):
        return 0
```

(`InvalidOperation` is already imported at the top of the module alongside `Decimal`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_gmv_max_api_client.py -v 2>&1 | tail -20`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/tiktok_marketing_api.py tests/test_gmv_max_api_client.py
git commit -F .git/COMMIT_MSG_DRAFT.txt   # "gmv-max: Marketing-API client (list campaigns, store ids, report)"
```

---

## Task 3: `gmv_max_sync.py` orchestration service

**Files:**
- Create: `app/services/gmv_max_sync.py`
- Test: `tests/test_gmv_max_sync.py`

- [ ] **Step 1: Write the failing test (chunker + parity + idempotency + edge cases)**

```python
# tests/test_gmv_max_sync.py
"""GMV-Max API sync: 30-day chunker, by-day aggregation/parity, idempotency,
and the no-credential / no-campaign paths. The Marketing-API seams are stubbed
so no network is touched."""
from datetime import date
from decimal import Decimal

import pytest

import app.services.gmv_max_sync as sync_mod
from app.db import Base, SessionLocal, engine
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.import_batch import ImportBatchStatus
from app.models.tiktok_marketing_credential import TikTokMarketingCredential


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_date_chunks_splits_into_30_day_windows():
    chunks = sync_mod._date_chunks(date(2026, 5, 1), date(2026, 6, 4), max_days=30)
    assert chunks == [
        (date(2026, 5, 1), date(2026, 5, 30)),
        (date(2026, 5, 31), date(2026, 6, 4)),
    ]


def test_date_chunks_single_window():
    assert sync_mod._date_chunks(date(2026, 5, 1), date(2026, 5, 10), max_days=30) == [
        (date(2026, 5, 1), date(2026, 5, 10)),
    ]


def _connect(db):
    db.add(TikTokMarketingCredential(access_token="tok", advertiser_id="adv1",
                                     advertiser_ids="adv1"))
    db.commit()


def _stub_api(monkeypatch, *, campaigns, stores, report_rows):
    monkeypatch.setattr(sync_mod.mapi, "list_gmv_max_campaigns",
                        lambda tok, adv: list(campaigns))
    monkeypatch.setattr(sync_mod.mapi, "gmv_max_store_ids",
                        lambda tok, adv, camps: list(stores))
    # report rows keyed by (start,end) chunk; default returns everything for any window
    monkeypatch.setattr(sync_mod.mapi, "get_gmv_max_report",
                        lambda tok, adv, st, s, e: list(report_rows))


def test_sync_aggregates_by_day_and_writes(monkeypatch):
    with SessionLocal() as db:
        _connect(db)
        _stub_api(monkeypatch,
                  campaigns=[{"campaign_id": "1"}],
                  stores=["STORE_A"],
                  report_rows=[
                      {"stat_day": "2026-05-10", "cost": Decimal("60.00"),
                       "orders": 3, "gross_revenue": Decimal("180.00")},
                      {"stat_day": "2026-05-10", "cost": Decimal("40.00"),
                       "orders": 2, "gross_revenue": Decimal("120.00")},  # 2nd campaign same day
                  ])
        batch = sync_mod.sync_gmv_max(db, lookback_days=35, today=date(2026, 5, 12))
        assert batch.status == ImportBatchStatus.COMPLETED
        rows = db.query(GmvMaxDailyMetric).all()
        assert len(rows) == 1
        assert rows[0].metric_date == date(2026, 5, 10)
        assert rows[0].cost == Decimal("100.00")          # 60 + 40
        assert rows[0].sku_orders == 5                     # 3 + 2
        assert rows[0].gross_revenue == Decimal("300.00")  # 180 + 120


def test_sync_is_idempotent(monkeypatch):
    rows = [{"stat_day": "2026-05-10", "cost": Decimal("100.00"),
             "orders": 5, "gross_revenue": Decimal("300.00")}]
    with SessionLocal() as db:
        _connect(db)
        _stub_api(monkeypatch, campaigns=[{"campaign_id": "1"}],
                  stores=["STORE_A"], report_rows=rows)
        sync_mod.sync_gmv_max(db, today=date(2026, 5, 12))
        sync_mod.sync_gmv_max(db, today=date(2026, 5, 12))
        all_rows = db.query(GmvMaxDailyMetric).all()
        assert len(all_rows) == 1                          # no duplicate-day growth
        assert all_rows[0].cost == Decimal("100.00")


def test_sync_no_credential_records_reason(monkeypatch):
    with SessionLocal() as db:
        batch = sync_mod.sync_gmv_max(db, today=date(2026, 5, 12))
        assert batch.status == ImportBatchStatus.FAILED
        assert "not connected" in (batch.error_message or "").lower()
        assert db.query(GmvMaxDailyMetric).count() == 0


def test_sync_no_campaigns_completes_zero(monkeypatch):
    with SessionLocal() as db:
        _connect(db)
        _stub_api(monkeypatch, campaigns=[], stores=[], report_rows=[])
        batch = sync_mod.sync_gmv_max(db, today=date(2026, 5, 12))
        assert batch.status == ImportBatchStatus.COMPLETED
        assert batch.rows_imported == 0
        assert "no gmv-max campaigns" in (batch.error_message or "").lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_gmv_max_sync.py -v 2>&1 | tail -25`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.gmv_max_sync'`.

- [ ] **Step 3: Create the service**

```python
# app/services/gmv_max_sync.py
"""Pull daily GMV-Max metrics from TikTok's Marketing API instead of a manual
"Campaign overview By-Day" CSV upload.

Discovers the advertiser's GMV-Max campaigns + their store ids, pulls
`/gmv_max/report/get/` over a trailing window (chunked into ≤30-day calls, the
API's max), aggregates the per-campaign/day rows into by-day totals, and feeds
them through the SAME writer as the CSV importer
(`gmv_max_campaign.import_dataframe`). Recorded as an `ImportBatch`
(kind TIKTOK_GMV_MAX) so it shows in Uploads history with a "last synced" time.
Idempotent: upsert by `metric_date`, so re-pulling recent (revised) days
overwrites in place.

Callable from the manual button (`routers/uploads.py`) and the weekday SAP
scheduler job (`services/scheduler.py`); both run it off the event loop. Never
raises — failures are recorded on the batch.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date as date_t
from datetime import timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy.orm import Session

from app.importers.gmv_max_campaign import import_dataframe
from app.models.import_batch import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    _utc_now_naive,
)
from app.services import tiktok_marketing_api as mapi

logger = logging.getLogger(__name__)

MAX_WINDOW_DAYS = 30  # /gmv_max/report/get/ rejects ranges wider than this


def _date_chunks(start: date_t, end: date_t, max_days: int = MAX_WINDOW_DAYS):
    """Split [start, end] (inclusive) into consecutive ≤max_days windows."""
    chunks = []
    cur = start
    while cur <= end:
        chunk_end = min(end, cur + timedelta(days=max_days - 1))
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def _aggregate(rows: list[dict]) -> pd.DataFrame:
    """Sum per-campaign/day report rows into one normalized row per day."""
    by_day: dict[date_t, dict] = defaultdict(
        lambda: {"cost": Decimal("0"), "sku_orders": 0, "gross_revenue": Decimal("0")})
    for r in rows:
        day = date_t.fromisoformat(r["stat_day"])
        agg = by_day[day]
        agg["cost"] += r["cost"]
        agg["sku_orders"] += int(r["orders"])
        agg["gross_revenue"] += r["gross_revenue"]
    return pd.DataFrame([
        {"metric_date": d, "cost": v["cost"], "sku_orders": v["sku_orders"],
         "gross_revenue": v["gross_revenue"]}
        for d, v in sorted(by_day.items())
    ])


def sync_gmv_max(db: Session, *, lookback_days: int = 35,
                 today: date_t | None = None) -> ImportBatch:
    """Pull the trailing `lookback_days` of GMV-Max metrics into
    GmvMaxDailyMetric. Returns the ImportBatch (COMPLETED or FAILED). Never
    raises: outcomes are recorded on the batch so the button/scheduler report
    cleanly."""
    today = today or date_t.today()
    start, end = today - timedelta(days=lookback_days), today
    ts = _utc_now_naive()

    batch = ImportBatch(
        kind=ImportFileKind.TIKTOK_GMV_MAX,
        status=ImportBatchStatus.PROCESSING,
        original_filename=f"TikTok GMV-Max API sync · {ts:%Y-%m-%d %H:%M}",
        stored_path="",
    )
    db.add(batch)
    db.flush()

    try:
        cred = mapi.get_credential(db)
        if cred is None or not cred.access_token:
            raise _SyncSkip("TikTok Marketing API not connected — connect it first.")

        token = cred.access_token
        all_rows: list[dict] = []
        found_campaigns = False
        for adv in mapi.advertiser_id_list(cred):
            campaigns = mapi.list_gmv_max_campaigns(token, adv)
            if not campaigns:
                continue
            found_campaigns = True
            store_ids = mapi.gmv_max_store_ids(token, adv, campaigns)
            if not store_ids:
                continue
            for chunk_start, chunk_end in _date_chunks(start, end):
                all_rows.extend(mapi.get_gmv_max_report(
                    token, adv, store_ids,
                    chunk_start.isoformat(), chunk_end.isoformat()))

        if not found_campaigns:
            batch.status = ImportBatchStatus.COMPLETED
            batch.rows_imported = 0
            batch.error_message = "No GMV-Max campaigns found for the connected advertiser(s)."
            batch.completed_at = _utc_now_naive()
            db.commit()
            return batch

        df = _aggregate(all_rows)
        res = import_dataframe(df, db, batch)
        note = (f"GMV-Max API sync: {res.rows_imported} days imported · "
                f"{res.rows_skipped} skipped · window {start}…{end}")
        batch.rows_imported = res.rows_imported
        batch.rows_skipped = res.rows_skipped
        batch.error_message = note + ("\n" + "\n".join(res.errors[:50]) if res.errors else "")
        batch.status = ImportBatchStatus.COMPLETED
        batch.completed_at = _utc_now_naive()
        db.commit()
        logger.info(note)
    except _SyncSkip as skip:
        db.rollback()
        batch.status = ImportBatchStatus.FAILED
        batch.error_message = str(skip)
        batch.completed_at = _utc_now_naive()
        db.add(batch)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        batch.status = ImportBatchStatus.FAILED
        batch.error_message = f"GMV-Max API sync failed: {exc}"
        batch.completed_at = _utc_now_naive()
        db.add(batch)
        db.commit()
        logger.exception("GMV-Max API sync failed")

    return batch


class _SyncSkip(Exception):
    """Expected non-error stop (e.g. not connected) — recorded, not logged as a crash."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_gmv_max_sync.py -v 2>&1 | tail -25`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/gmv_max_sync.py tests/test_gmv_max_sync.py
git commit -F .git/COMMIT_MSG_DRAFT.txt   # "gmv-max: API sync service (discover, chunk, aggregate, upsert)"
```

---

## Task 4: Weekday SAP scheduler job also runs the GMV-Max pull

**Files:**
- Modify: `app/services/scheduler.py:35-42` (`_run_inventory_sync_job`)
- Test: `tests/test_gmv_max_scheduler.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gmv_max_scheduler.py
"""The existing weekday SAP scheduler job also pulls GMV-Max. A GMV-Max failure
must NOT abort the inventory sync (independent try/except)."""
import app.services.scheduler as sched


def test_inventory_job_also_runs_gmv_max(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.inventory_sync.sync_inventory_from_sap",
                        lambda db, source="scheduled": calls.append(("inv", source)))
    monkeypatch.setattr("app.services.gmv_max_sync.sync_gmv_max",
                        lambda db, source=None: calls.append(("gmv",)))
    sched._run_inventory_sync_job()
    assert ("inv", "scheduled") in calls
    assert ("gmv",) in calls


def test_gmv_max_failure_does_not_abort_inventory(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.inventory_sync.sync_inventory_from_sap",
                        lambda db, source="scheduled": calls.append("inv"))

    def boom(db, source=None):
        raise RuntimeError("gmv exploded")

    monkeypatch.setattr("app.services.gmv_max_sync.sync_gmv_max", boom)
    sched._run_inventory_sync_job()           # must not raise
    assert "inv" in calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_gmv_max_scheduler.py -v 2>&1 | tail -20`
Expected: FAIL — GMV-Max sync is not called (only `inv` recorded), or `sync_gmv_max` import target missing.

Note: `sync_gmv_max` is called with no `source` kwarg in the job; the test stubs accept `source=None` to match. The real `sync_gmv_max` signature has no `source` param, so the job calls it positionally as `sync_gmv_max(db)`.

- [ ] **Step 3: Modify the job**

In `app/services/scheduler.py`, replace `_run_inventory_sync_job`:

```python
def _run_inventory_sync_job() -> None:
    """Scheduler entry point: own DB session, never propagate exceptions. Runs the
    SAP inventory sync AND the GMV-Max API pull on the same weekday schedule; each
    is independent so one failing never aborts the other (both also record their
    own failures)."""
    from app.services.gmv_max_sync import sync_gmv_max
    from app.services.inventory_sync import sync_inventory_from_sap

    with SessionLocal() as db:
        try:
            sync_inventory_from_sap(db, source="scheduled")
        except Exception:  # noqa: BLE001
            logger.exception("scheduled SAP inventory sync failed")
        try:
            sync_gmv_max(db)
        except Exception:  # noqa: BLE001
            logger.exception("scheduled GMV-Max sync failed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_gmv_max_scheduler.py -v 2>&1 | tail -20`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/scheduler.py tests/test_gmv_max_scheduler.py
git commit -F .git/COMMIT_MSG_DRAFT.txt   # "gmv-max: weekday SAP scheduler job also pulls GMV-Max"
```

---

## Task 5: Manual "Sync GMV-Max" button on the Uploads page

**Files:**
- Modify: `app/routers/uploads.py` (add the POST route + `last_gmv_sync` context near `last_sap_sync`)
- Modify: `app/templates/uploads.html` (add a card after the SAP card)
- Test: `tests/test_gmv_max_button.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gmv_max_button.py
"""Manual GMV-Max sync button: posts off the event loop, 303s back to /uploads,
and the Uploads page shows a GMV-Max feed card."""
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


def test_uploads_page_shows_gmv_card(client):
    r = client.get("/uploads")
    assert r.status_code == 200
    assert "Live GMV-Max feed (TikTok API)" in r.text
    assert "/uploads/sync-gmv-max" in r.text


def test_sync_button_calls_service_and_redirects(client, monkeypatch):
    called = {}
    monkeypatch.setattr("app.routers.uploads.sync_gmv_max",
                        lambda db, **kw: called.setdefault("hit", True))
    r = client.post("/uploads/sync-gmv-max", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/uploads"
    assert called.get("hit") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_gmv_max_button.py -v 2>&1 | tail -20`
Expected: FAIL — card text / route absent.

- [ ] **Step 3: Add the route + context in `app/routers/uploads.py`**

Add the import near the existing `from app.services.inventory_sync import sync_inventory_from_sap` (line ~20):

```python
from app.services.gmv_max_sync import sync_gmv_max
```

In the `/uploads` GET handler, alongside `last_sap_sync` (after line ~115), add:

```python
    # Last GMV-Max API sync = most recent TIKTOK_GMV_MAX batch produced by the
    # API (its filename starts with "TikTok GMV-Max API"), for the feed card.
    last_gmv_sync = next(
        (b for b in batches_by_kind[ImportFileKind.TIKTOK_GMV_MAX]
         if (b.original_filename or "").startswith("TikTok GMV-Max API")),
        None,
    )
```

Add `"last_gmv_sync": last_gmv_sync,` to the TemplateResponse context dict.

Add the POST route after `sync_inventory_sap` (after line ~135):

```python
@router.post("/uploads/sync-gmv-max")
async def sync_gmv_max_button(db: Session = Depends(get_db)):
    """Manual 'Sync GMV-Max' button. Runs the API pull + import in a worker thread
    so the event loop isn't blocked, then returns to /uploads."""
    await run_in_threadpool(sync_gmv_max, db)
    return RedirectResponse("/uploads", status_code=303)
```

- [ ] **Step 4: Add the card in `app/templates/uploads.html`**

Immediately after the closing of the SAP card block (the `{# ── Live inventory feed (SAP) … #}` section), add a parallel card. Match the SAP card's markup; the status pill mirrors it:

```html
{# ── Live GMV-Max feed (TikTok Marketing API) ──────────────────────────── #}
<div class="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
  <div class="flex items-start justify-between gap-3">
    <div>
      <h3 class="flex items-center gap-2 text-sm font-semibold text-slate-800">
        {{ ui.icon("refresh-cw", "h-4 w-4 text-slate-500") }}
        Live GMV-Max feed (TikTok API)
      </h3>
      <p class="mt-0.5 text-xs text-slate-500">Pulls daily GMV-Max ad spend, orders &amp; attributed revenue straight from TikTok into the Ad Spend report — no CSV needed.</p>
    </div>
    <form action="/uploads/sync-gmv-max" method="post" id="gmv-sync-form">
      <button type="submit"
        class="inline-flex items-center gap-1.5 rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700">
        Sync GMV-Max now
      </button>
    </form>
  </div>
  {% if last_gmv_sync %}
    <div class="mt-3 flex items-center gap-2 text-xs">
      <span class="rounded px-1.5 py-0.5 font-medium
        {% if last_gmv_sync.status.value == 'completed' %}bg-emerald-50 text-emerald-700
        {% elif last_gmv_sync.status.value == 'failed' %}bg-rose-50 text-rose-700
        {% else %}bg-slate-100 text-slate-600{% endif %}">{{ last_gmv_sync.status.value }}</span>
      <span class="text-sm text-slate-700">{{ last_gmv_sync.uploaded_at.strftime("%Y-%m-%d %H:%M") }} UTC</span>
      {% if last_gmv_sync.error_message %}
        <span class="text-slate-500">· {{ last_gmv_sync.error_message.split("\n")[0] }}</span>
      {% endif %}
    </div>
  {% endif %}
</div>
```

The `refresh-cw` icon is already committed (`app/static/icons/refresh-cw.svg`), so the icon-guard test passes with no vendoring needed.

- [ ] **Step 5: Run tests to verify they pass**

Run: `py -m pytest tests/test_gmv_max_button.py -v 2>&1 | tail -20`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the icon-guard + uploads tests (regression)**

Run: `py -m pytest -k "icon or uploads" -q 2>&1 | tail -15`
Expected: PASS (every `ui.icon()` reference has a committed SVG).

- [ ] **Step 7: Commit**

```bash
git add app/routers/uploads.py app/templates/uploads.html tests/test_gmv_max_button.py app/static/icons/
git commit -F .git/COMMIT_MSG_DRAFT.txt   # "gmv-max: manual sync button + feed card on Uploads"
```

---

## Task 6: Full-suite green + manual prod-data parity check

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `py -m pytest 2>&1 | tail -15`
Expected: all pass (prior baseline 790 passed, 11 skipped + the new tests).

- [ ] **Step 2: Live parity smoke (against prod credentials, read-only)**

Run a one-off invocation of the sync logic against prod for a settled month and confirm it ties to the uploaded CSV (already proven manually: May → cost `7824.02`, gross_revenue `15769.65`). This is a read/aggregation check — it does NOT need to write. Use the same `fly ssh console` base64 pattern used during discovery, calling `mapi.list_gmv_max_campaigns` → `gmv_max_store_ids` → `get_gmv_max_report` over May and summing. Confirm the totals match before deploying.

- [ ] **Step 3: Deploy + verify**

Per the repo deploy flow (local merge, no PR): push the branch, `git checkout main`, `git pull --ff-only`, `git merge --no-ff feature/gmv-max-auto-pull`, `git push origin main`, `fly deploy`. After release, click "Sync GMV-Max now" on prod `/uploads`, confirm the card shows `completed` and the Ad Spend page reflects current-day spend. Confirm via `fly releases` + a prod page check, NOT a machine restart.

---

## Self-Review

**Spec coverage:**
- Discovery (campaigns + store ids) → Task 2 (`list_gmv_max_campaigns`, `gmv_max_store_ids`) + Task 3 (loop). ✓
- Report pull, ≤30-day chunks, pagination → Task 2 (`get_gmv_max_report` pagination) + Task 3 (`_date_chunks`). ✓
- Aggregate campaign×day → by-day → Task 3 (`_aggregate`). ✓
- Shared `import_dataframe` writer, idempotent by `metric_date` → Task 1. ✓
- Manual button (threadpool, 303) → Task 5. ✓
- Weekday SAP cron also runs GMV-Max, independent failure → Task 4. ✓
- CSV fallback unchanged → Task 1 keeps `run()` behavior (regression check Step 5). ✓
- Look-back 35 days default → Task 3 `lookback_days=35`. ✓
- Error handling (no cred / no campaigns / API error / rollback) → Task 3 (`_SyncSkip`, no-campaign branch, broad except + rollback). ✓
- Testing (chunker, aggregation/parity, idempotency, discovery edges, seam) → Tasks 1–5 tests + Task 6 live parity. ✓
- Out-of-scope items (no new schedule fields, by-day not per-campaign, no shop_id beyond CSV parity, no roi/net_cost columns) honored. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. Icon step has a concrete fallback if `megaphone.svg` isn't vendored. ✓

**Type consistency:** Normalized frame columns `metric_date`/`cost`/`sku_orders`/`gross_revenue` are identical across Task 1 (`import_dataframe`, `_upsert_row`) and Task 3 (`_aggregate`). Report-row dict keys `stat_day`/`cost`/`orders`/`gross_revenue` are identical across Task 2 (`get_gmv_max_report`) and Task 3 (`_aggregate`). `sync_gmv_max(db, *, lookback_days, today)` called positionally as `sync_gmv_max(db)` in Task 4 and `run_in_threadpool(sync_gmv_max, db)` in Task 5 — consistent (no `source` param). ✓

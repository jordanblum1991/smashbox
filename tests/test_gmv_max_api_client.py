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
            {"store_id": "STORE_A"},   # same store -> deduped
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

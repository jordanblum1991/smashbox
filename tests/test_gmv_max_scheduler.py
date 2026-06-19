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

"""GMV-Max (Marketing API) sync runs as its OWN scheduled job, decoupled from
the SAP inventory job, on a user-editable schedule."""
import app.services.scheduler as sched
from app.models.shop import Shop


def test_inventory_job_no_longer_runs_gmv_max(monkeypatch):
    """After decoupling, the inventory job runs ONLY the inventory sync."""
    calls = []
    monkeypatch.setattr("app.services.inventory_sync.sync_inventory_from_sap",
                        lambda db, source="scheduled": calls.append(("inv", source)))
    monkeypatch.setattr("app.services.gmv_max_sync.sync_gmv_max",
                        lambda db, source=None: calls.append(("gmv",)))
    sched._run_inventory_sync_job()
    assert ("inv", "scheduled") in calls
    assert ("gmv",) not in calls


def test_gmv_sync_job_runs_gmv_max(monkeypatch):
    """The dedicated GMV-Max job pulls the ad data (and never raises)."""
    calls = []
    monkeypatch.setattr("app.services.gmv_max_sync.sync_gmv_max",
                        lambda db, source=None: calls.append("gmv"))
    sched._run_gmv_sync_job()
    assert "gmv" in calls


def test_apply_gmv_schedule_is_noop_when_scheduler_off():
    """Safe to call when the scheduler isn't running (dev/tests) — no error."""
    sched.apply_gmv_schedule(Shop(timezone="America/Los_Angeles"))

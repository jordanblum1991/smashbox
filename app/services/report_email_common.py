# app/services/report_email_common.py
"""Shared pieces for the per-report email features (Sales, Samples): inline email
CSS, the rolling-period resolver for scheduled sends, and a generic APScheduler
registration helper. Inventory keeps its own copy of the styles + its own scheduler
function (left untouched — it is in prod)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Inline styles approximating the dashboard (email clients strip CSS classes).
CARD = ("border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;font-family:"
        "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:760px")
HEADER = "padding:16px 12px;background:#f8fafc"
H_TITLE = "font-size:16px;font-weight:700;color:#0f172a"
H_SUB = "font-size:12px;color:#475569;margin-top:2px"
TH = ("padding:8px 12px;text-align:left;font-size:10px;font-weight:600;text-transform:"
      "uppercase;letter-spacing:.05em;color:#64748b;border-bottom:1px solid #e2e8f0")
TH_R = TH + ";text-align:right"
TD = "padding:8px 12px;font-size:13px;color:#0f172a;border-bottom:1px solid #f1f5f9"
TD_R = TD + ";text-align:right;font-variant-numeric:tabular-nums"
TOT = "padding:8px 12px;font-size:13px;font-weight:700;color:#0f172a;border-top:2px solid #e2e8f0"
TOT_R = TOT + ";text-align:right;font-variant-numeric:tabular-nums"

# (key → label). The per-report allow-lists pick which keys each report offers.
ROLLING_PERIODS = {
    "prev_month": "Previous month",
    "mtd": "Month-to-date",
    "prev_week": "Previous week (Mon–Sun)",
    "last_7": "Last 7 days",
    "last_30": "Last 30 days",
    "prev_fiscal_month": "Previous fiscal month",
}
SALES_PERIODS = ["prev_month", "mtd", "prev_week", "last_7", "last_30", "prev_fiscal_month"]
SAMPLE_PERIODS = ["prev_month", "mtd"]   # month-granular report → month-level only


@dataclass(frozen=True)
class RollingWindow:
    start: date                       # inclusive (calendar)
    end: date                         # inclusive
    label: str
    fiscal_ym: tuple[int, int] | None = None   # set only for prev_fiscal_month


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def resolve_rolling_period(key: str, *, today: date) -> RollingWindow:
    """Recompute the concrete inclusive [start, end] for a rolling-window key,
    relative to `today` (shop-local). Unknown key → prev_month."""
    label = ROLLING_PERIODS.get(key, ROLLING_PERIODS["prev_month"])
    if key == "mtd":
        return RollingWindow(_first_of_month(today), today, label)
    if key == "last_7":
        return RollingWindow(today - timedelta(days=6), today, label)
    if key == "last_30":
        return RollingWindow(today - timedelta(days=29), today, label)
    if key == "prev_week":
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        return RollingWindow(last_monday, last_monday + timedelta(days=6), label)
    if key == "prev_fiscal_month":
        from app.reports.sales_report import current_fiscal_ym
        fy, fm = current_fiscal_ym(today)                 # current fiscal month
        pfy, pfm = (fy, fm - 1) if fm > 1 else (fy - 1, 12)
        end = date(pfy, pfm, 28)                          # fiscal month closes on the 28th
        start = date(pfy - 1, 12, 29) if pfm == 1 else date(pfy, pfm - 1, 29)
        return RollingWindow(start, end, label, fiscal_ym=(pfy, pfm))
    # prev_month (default)
    last_prev = _first_of_month(today) - timedelta(days=1)
    return RollingWindow(_first_of_month(last_prev), last_prev, label)


def register_report_job(scheduler, job_id, *, enabled, recipients, days, hour,
                        minute, timezone, run_fn) -> None:
    """Add/replace or remove a report-email cron job to match config. No-op when
    the scheduler isn't running. Registered only when enabled AND recipients exist."""
    if scheduler is None:
        return
    if not (enabled and recipients):
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        return
    from apscheduler.triggers.cron import CronTrigger
    scheduler.add_job(
        run_fn,
        trigger=CronTrigger(day_of_week=days, hour=hour, minute=minute, timezone=timezone),
        id=job_id, replace_existing=True, coalesce=True,
        misfire_grace_time=3600, max_instances=1,
    )

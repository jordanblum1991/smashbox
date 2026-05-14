"""Dashboard home — KPI tiles + recent imports, scoped by a period selector.

Uses the same compute_pnl_view as /reports/pnl, so dashboard numbers
always tie to the P&L page for the selected period.
"""
from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.import_batch import ImportBatch
from app.reports.pnl import PeriodKind, compute_pnl_view
from app.reports.settlement_only_orders import count_settlement_only_orders
from app.reports.unmapped_skus import find_unmapped_skus
from app.templating import templates

router = APIRouter(tags=["dashboard"])


@router.get("/")
def home(
    request: Request,
    period: PeriodKind = PeriodKind.MONTH,
    year: int | None = None,
    month: int | None = None,
    start_year: int | None = None,
    start_month: int | None = None,
    end_year: int | None = None,
    end_month: int | None = None,
    db: Session = Depends(get_db),
):
    view = compute_pnl_view(
        db, period, year, month,
        start_year=start_year, start_month=start_month,
        end_year=end_year, end_month=end_month,
    )

    # Unmapped + orphans are catalog-wide, not period-scoped.
    unmapped = find_unmapped_skus(db)
    orphan_orders = count_settlement_only_orders(db)
    recent = (
        db.query(ImportBatch)
        .order_by(ImportBatch.uploaded_at.desc())
        .limit(5)
        .all()
    )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "view": view,
            "pnl": view.total,            # convenience for existing tile/waterfall code
            "unmapped_count": len(unmapped),
            "orphan_count": orphan_orders,
            "recent": recent,
            "today": date.today(),
        },
    )

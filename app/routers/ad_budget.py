"""Admin CRUD + report for the Ad Budget tracking tool.

A budget is a flexible date range + allocated amount. Actual spend auto-pulls
from the daily GMV-Max ad cost over that range (see app/reports/ad_budget.py);
manual dated promotions carve out part of the budget. Mirrors the validation /
303-flash discipline of app/routers/purchase_invoices.py.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_admin
from app.db import get_db
from app.models.ad_budget import AdBudget, AdBudgetPromotion
from app.reports.ad_budget import (
    compute_budget_view,
    current_budget,
    list_budgets,
)
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])

# First budget per the spec — the program starts on this date.
_DEFAULT_START = date(2026, 7, 1)


def _err_redirect(path: str, reason: str, **form: str) -> RedirectResponse:
    params: dict[str, str] = {"error": reason}
    for k, v in form.items():
        if v is not None and v != "":
            params[k] = v
    return RedirectResponse(f"{path}?{urlencode(params)}", status_code=303)


def _parse_date(raw: str, label: str) -> tuple[date | None, str | None]:
    raw = (raw or "").strip()
    if not raw:
        return None, f"{label} is required."
    try:
        return date.fromisoformat(raw), None
    except ValueError:
        return None, f"{label} must be a valid date (YYYY-MM-DD)."


def _parse_amount(raw: str, label: str) -> tuple[Decimal | None, str | None]:
    raw = (raw or "").strip()
    if not raw:
        return None, f"{label} is required."
    try:
        amt = Decimal(raw)
    except InvalidOperation:
        return None, f"{label} must be a number (got {raw!r})."
    if amt <= 0:
        return None, f"{label} must be greater than 0."
    return amt.quantize(Decimal("0.01")), None


def _get_budget(db: Session, budget_id: int) -> AdBudget:
    budget = db.execute(
        select(AdBudget)
        .options(selectinload(AdBudget.promotions))
        .where(AdBudget.id == budget_id)
    ).scalar_one_or_none()
    if budget is None:
        raise HTTPException(status_code=404, detail="Budget not found")
    return budget


# --- List -------------------------------------------------------------------

@router.get("/ad-budget", dependencies=[Depends(require_admin)])
def ad_budget_list(
    request: Request, db: Session = Depends(get_db),
    error: str | None = None, notice: str | None = None,
) -> Response:
    budgets = list_budgets(db)
    current = current_budget(db)
    # Lightweight per-row summary for the list (reuses the report engine).
    summaries = {b.id: compute_budget_view(db, b) for b in budgets}
    return templates.TemplateResponse(
        request, "admin/ad_budget_list.html",
        {"budgets": budgets, "summaries": summaries,
         "current_id": current.id if current else None,
         "error": error, "notice": notice},
    )


# --- Create -----------------------------------------------------------------

@router.get("/ad-budget/new", dependencies=[Depends(require_admin)])
def ad_budget_new_form(
    request: Request,
    error: str | None = None,
    label: str | None = None, start_date: str | None = None,
    end_date: str | None = None, amount: str | None = None,
) -> Response:
    return templates.TemplateResponse(
        request, "admin/ad_budget_new.html",
        {"error": error,
         "label": label or "", "amount": amount or "",
         "start_date": start_date or _DEFAULT_START.isoformat(),
         "end_date": end_date or ""},
    )


@router.post("/ad-budget", dependencies=[Depends(require_admin)])
def ad_budget_create(
    label: str = Form(""), start_date: str = Form(""),
    end_date: str = Form(""), amount: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    new = "/admin/ad-budget/new"
    label_clean = (label or "").strip()
    if not label_clean:
        return _err_redirect(new, "Label is required.", start_date=start_date, end_date=end_date, amount=amount)
    sd, err = _parse_date(start_date, "Start date")
    if err:
        return _err_redirect(new, err, label=label, end_date=end_date, amount=amount)
    ed, err = _parse_date(end_date, "End date")
    if err:
        return _err_redirect(new, err, label=label, start_date=start_date, amount=amount)
    if ed < sd:
        return _err_redirect(new, "End date must be on or after the start date.",
                             label=label, start_date=start_date, end_date=end_date, amount=amount)
    amt, err = _parse_amount(amount, "Budget amount")
    if err:
        return _err_redirect(new, err, label=label, start_date=start_date, end_date=end_date)

    budget = AdBudget(label=label_clean, start_date=sd, end_date=ed, amount=amt)
    db.add(budget)
    db.commit()
    return RedirectResponse(f"/admin/ad-budget/{budget.id}?notice=Budget+created", status_code=303)


# --- CSV export (before the bare {id} route so '.csv' isn't read as an id) ---

@router.get("/ad-budget/{budget_id}.csv", dependencies=[Depends(require_admin)])
def ad_budget_csv(budget_id: int, db: Session = Depends(get_db)) -> Response:
    budget = _get_budget(db, budget_id)
    view = compute_budget_view(db, budget)
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Ad Budget: {budget.label}",
                f"{budget.start_date.isoformat()} to {budget.end_date.isoformat()}"])
    w.writerow(["Budget", f"{view.budget_amount:.2f}"])
    w.writerow(["Ad spent", f"{view.total_ad_spend:.2f}"])
    w.writerow(["Promotions", f"{view.total_promotions:.2f}"])
    w.writerow(["Available remaining", f"{view.available:.2f}"])
    w.writerow([])
    w.writerow(["Date", "Ad Spend", "Promotions", "Committed to date", "Available remaining"])
    for r in view.rows:
        w.writerow([r.day.isoformat(), f"{r.ad_spend:.2f}", f"{r.promotions:.2f}",
                    f"{r.committed_to_date:.2f}", f"{r.available:.2f}"])
    fname = f"ad_budget_{budget.label.replace(' ', '_')}.csv"
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# --- Detail report ----------------------------------------------------------

@router.get("/ad-budget/{budget_id}", dependencies=[Depends(require_admin)])
def ad_budget_detail(
    request: Request, budget_id: int, db: Session = Depends(get_db),
    error: str | None = None, notice: str | None = None,
) -> Response:
    budget = _get_budget(db, budget_id)
    view = compute_budget_view(db, budget)
    return templates.TemplateResponse(
        request, "admin/ad_budget_detail.html",
        {"view": view, "budget": budget, "error": error, "notice": notice},
    )


@router.post("/ad-budget/{budget_id}/edit", dependencies=[Depends(require_admin)])
def ad_budget_edit(
    budget_id: int,
    label: str = Form(""), start_date: str = Form(""),
    end_date: str = Form(""), amount: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    budget = _get_budget(db, budget_id)
    detail = f"/admin/ad-budget/{budget_id}"
    label_clean = (label or "").strip()
    if not label_clean:
        return _err_redirect(detail, "Label is required.")
    sd, err = _parse_date(start_date, "Start date")
    if err:
        return _err_redirect(detail, err)
    ed, err = _parse_date(end_date, "End date")
    if err:
        return _err_redirect(detail, err)
    if ed < sd:
        return _err_redirect(detail, "End date must be on or after the start date.")
    amt, err = _parse_amount(amount, "Budget amount")
    if err:
        return _err_redirect(detail, err)
    budget.label, budget.start_date, budget.end_date, budget.amount = label_clean, sd, ed, amt
    db.commit()
    return RedirectResponse(f"{detail}?notice=Budget+updated", status_code=303)


# --- Promotions -------------------------------------------------------------

@router.post("/ad-budget/{budget_id}/promotions", dependencies=[Depends(require_admin)])
def ad_budget_add_promotion(
    budget_id: int,
    name: str = Form(""), amount: str = Form(""),
    promo_date: str = Form(""), note: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    budget = _get_budget(db, budget_id)
    detail = f"/admin/ad-budget/{budget_id}"
    name_clean = (name or "").strip()
    if not name_clean:
        return _err_redirect(detail, "Promotion name is required.")
    amt, err = _parse_amount(amount, "Promotion amount")
    if err:
        return _err_redirect(detail, err)
    pd, err = _parse_date(promo_date, "Promotion date")
    if err:
        return _err_redirect(detail, err)
    if pd < budget.start_date or pd > budget.end_date:
        return _err_redirect(
            detail,
            f"Promotion date must be within the budget period "
            f"({budget.start_date.isoformat()} to {budget.end_date.isoformat()}).",
        )
    db.add(AdBudgetPromotion(
        ad_budget_id=budget.id, name=name_clean, amount=amt,
        promo_date=pd, note=(note or "").strip() or None,
    ))
    db.commit()
    return RedirectResponse(f"{detail}?notice=Promotion+added", status_code=303)


@router.post(
    "/ad-budget/{budget_id}/promotions/{promo_id}/delete",
    dependencies=[Depends(require_admin)],
)
def ad_budget_delete_promotion(
    budget_id: int, promo_id: int, db: Session = Depends(get_db),
) -> Response:
    promo = db.execute(
        select(AdBudgetPromotion)
        .where(AdBudgetPromotion.id == promo_id, AdBudgetPromotion.ad_budget_id == budget_id)
    ).scalar_one_or_none()
    if promo is not None:
        db.delete(promo)
        db.commit()
    return RedirectResponse(f"/admin/ad-budget/{budget_id}?notice=Promotion+removed", status_code=303)

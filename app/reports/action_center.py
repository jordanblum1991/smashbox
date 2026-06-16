"""Action Center — one consolidated view of everything that needs attention.

Rolls up the signals already scattered across the nav badges and dashboard
banners (overdue/soon payables, low-stock reorders, data-health issues) into a
single prioritized page, plus a few extras (failed imports, stale data sources,
open POs, order-coverage gaps). Pure computation — reuses the same underlying
count functions as the badges, so the Action Center can never drift from them.

`total_items` counts every actionable item across the groups (payables,
inventory, data health, imports). The nav badge is a cheaper approximation
computed from the per-request state signals (no extra queries), so it can differ
slightly when a failed/stale import is also open. Open POs and coverage gaps are
informational ("heads up") and deliberately excluded from the headline count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.import_batch import ImportBatch, ImportBatchStatus
from app.reports.coverage_gaps import compute_order_coverage
from app.reports.in_transit import in_transit_summary
from app.reports.inventory_alerts import get_inventory_alert_summary
from app.reports.missing_cogs import count_missing_cogs
from app.reports.overdue_ap import compute_due_soon_ap, compute_overdue_ap
from app.reports.policy_violations import count_policy_violations
from app.reports.reconciliation import get_recon_break_summary
from app.reports.settlement_only_orders import count_settlement_only_orders
from app.reports.unmapped_skus import count_unmapped_skus
from app.services.data_freshness import compute_freshness
from app.services.reporting_tz import now_local


@dataclass
class ActionItem:
    key: str          # stable id for tests
    severity: str     # "error" | "warn" | "info"
    title: str
    count: int
    detail: str
    href: str
    cta: str = "Open"


@dataclass
class ActionGroup:
    key: str
    title: str
    icon: str
    items: list[ActionItem] = field(default_factory=list)


_SEV_RANK = {"error": 0, "warn": 1, "info": 2}


def _money(v: Decimal) -> str:
    return f"${v:,.2f}"


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or singular + "s")


def compute_action_center(db: Session) -> "ActionCenterView":
    groups: list[ActionGroup] = []

    # --- Payables -----------------------------------------------------------
    pay = ActionGroup("payables", "Payables", "circle-alert")
    oap = compute_overdue_ap(db)
    if oap["count"] > 0:
        pay.items.append(ActionItem(
            "ap_overdue", "error",
            f"{oap['count']} overdue {_plural(oap['count'], 'invoice')}",
            oap["count"], f"{_money(oap['total'])} past due to Smashbox.",
            "/admin/product-invoices/aging", "Review aging",
        ))
    soon = compute_due_soon_ap(db)
    if soon["count"] > 0:
        pay.items.append(ActionItem(
            "ap_due_soon", "warn",
            f"{soon['count']} {_plural(soon['count'], 'invoice')} due soon",
            soon["count"],
            f"{_money(soon['total'])} due within {soon['within_days']} days.",
            "/admin/product-invoices/aging", "Review aging",
        ))

    # --- Inventory ----------------------------------------------------------
    inv_group = ActionGroup("inventory", "Inventory", "package")
    inv = get_inventory_alert_summary(db)
    if inv.get("out_of_stock", 0) > 0:
        n = inv["out_of_stock"]
        inv_group.items.append(ActionItem(
            "inv_out_of_stock", "error", f"{n} {_plural(n, 'SKU')} out of stock",
            n, "Sellable stock is at zero — reorder to avoid lost sales.",
            "/reports/demand-planning", "Open Demand Planning",
        ))
    if inv.get("reorder_now", 0) > 0:
        n = inv["reorder_now"]
        inv_group.items.append(ActionItem(
            "inv_reorder_now", "warn", f"{n} {_plural(n, 'SKU')} at reorder point",
            n, "At or below the reorder point — place a PO now.",
            "/reports/demand-planning", "Open Demand Planning",
        ))
    if inv.get("at_risk", 0) > 0:
        n = inv["at_risk"]
        inv_group.items.append(ActionItem(
            "inv_at_risk", "warn", f"{n} {_plural(n, 'SKU')} at risk",
            n, "Projected to cross the reorder point soon.",
            "/reports/demand-planning", "Open Demand Planning",
        ))

    # --- Data health --------------------------------------------------------
    dh = ActionGroup("data_health", "Data health", "triangle-alert")
    unmapped = count_unmapped_skus(db)
    if unmapped > 0:
        dh.items.append(ActionItem(
            "dh_unmapped", "warn", f"{unmapped} unmapped {_plural(unmapped, 'SKU')}",
            unmapped, "TikTok SKU IDs missing from the catalog — add them for COGS + names.",
            "/reports/unmapped-skus", "View",
        ))
    missing = count_missing_cogs(db)
    if missing > 0:
        dh.items.append(ActionItem(
            "dh_missing_cogs", "warn", f"{missing} {_plural(missing, 'SKU')} missing COGS",
            missing, "Zero unit COGS distorts gross profit — set a cost.",
            "/reports/recon-health?tab=data-health", "View",
        ))
    policy = count_policy_violations(db)
    if policy > 0:
        dh.items.append(ActionItem(
            "dh_policy", "warn",
            f"{policy} discount policy {_plural(policy, 'violation')}",
            policy, "Seller-funded discount over the 30% ceiling — review.",
            "/reports/policy-violations", "Review",
        ))
    orphans = count_settlement_only_orders(db)
    if orphans > 0:
        dh.items.append(ActionItem(
            "dh_orphans", "info",
            f"{orphans} settlement-only {_plural(orphans, 'order')}",
            orphans, "Settled orders with no matching orders-export row.",
            "/reports/settlement-only-orders", "View",
        ))
    recon = get_recon_break_summary(db)
    if recon["count"] > 0:
        n = recon["count"]
        dh.items.append(ActionItem(
            "dh_recon_break", "warn",
            f"{n} {_plural(n, 'day')} don't reconcile",
            n,
            f"GMV differs from TikTok's reported total on {n} settled "
            f"{_plural(n, 'day')} — worst {_money(abs(recon['worst_variance']))} "
            f"on {recon['worst_day']}.",
            "/reports/recon-health?tab=recon", "Open reconciliation",
        ))

    # --- Imports & data freshness ------------------------------------------
    imp = ActionGroup("imports", "Imports & data", "upload")
    last_failed = (
        db.query(ImportBatch)
        .filter(ImportBatch.status == ImportBatchStatus.FAILED)
        .order_by(ImportBatch.uploaded_at.desc())
        .first()
    )
    if last_failed is not None:
        imp.items.append(ActionItem(
            "import_failed", "error", "Last import failed", 1,
            f"{last_failed.original_filename} · {last_failed.kind.value}",
            "/uploads", "View imports",
        ))
    # Only "stale" (imported before, now >7 days old) — NOT "missing". A source
    # the shop never uses would otherwise flag forever; staleness means a source
    # you DO use has gone old.
    stale_sources = [
        f.label for f in compute_freshness(db) if f.staleness == "stale"
    ]
    if stale_sources:
        n = len(stale_sources)
        imp.items.append(ActionItem(
            "data_stale", "warn",
            f"{n} data {_plural(n, 'source')} stale", n,
            ", ".join(stale_sources) + " — re-import to keep reports current.",
            "/uploads", "Manage imports",
        ))

    # TikTok auto-sync health — a stream in `error`, or a sync that hasn't run in
    # >36h (past the daily cadence). Only fires when connected. Shares one helper
    # with the dashboard's per-request action-items count so they never drift.
    from app.services.tiktok_sync import sync_health

    health = sync_health(db)
    if health and health["reason"] == "error":
        streams = health["streams"]
        imp.items.append(ActionItem(
            "tiktok_sync_error", "error", "TikTok sync error", len(streams),
            f"{', '.join(streams)} failed on the last run — re-check the connection.",
            "/admin/tiktok", "Open connection",
        ))
    elif health and health["reason"] == "stale":
        imp.items.append(ActionItem(
            "tiktok_sync_stale", "warn", "TikTok auto-sync stale", 1,
            f"Last ran {health['hours']}h ago — the daily sync may have stalled.",
            "/admin/tiktok", "Open connection",
        ))

    for g in (pay, inv_group, dh, imp):
        g.items.sort(key=lambda i: _SEV_RANK.get(i.severity, 9))
        if g.items:
            groups.append(g)

    # Headline counts only the actionable items (excludes the "heads up" group),
    # matching the nav badge.
    total_items = sum(len(g.items) for g in groups)
    error_count = sum(1 for g in groups for i in g.items if i.severity == "error")
    warn_count = sum(1 for g in groups for i in g.items if i.severity == "warn")

    # --- Heads up (informational, not counted in the headline) --------------
    heads_up = ActionGroup("heads_up", "Heads up", "info")
    po = in_transit_summary(db)
    if po["open_pos"] > 0:
        heads_up.items.append(ActionItem(
            "open_pos", "info",
            f"{po['open_pos']} open purchase {_plural(po['open_pos'], 'order')}",
            po["open_pos"],
            f"{po['units_on_order']:,} {_plural(po['units_on_order'], 'unit')} on order, "
            "counted as in-transit in Demand Planning.",
            "/admin/purchase-orders", "View POs",
        ))
    cov = compute_order_coverage(db)
    if cov.missing_days > 0:
        heads_up.items.append(ActionItem(
            "coverage_gaps", "info",
            f"{cov.missing_days} {_plural(cov.missing_days, 'day')} with no orders",
            cov.missing_days,
            f"Gaps in the active order range ({len(cov.gaps)} "
            f"{_plural(len(cov.gaps), 'gap')}) — may indicate a missing import.",
            "/reports/recon-health?tab=recon", "Reconciliation",
        ))

    return ActionCenterView(
        groups=groups,
        heads_up=heads_up.items,
        total_items=total_items,
        error_count=error_count,
        warn_count=warn_count,
        as_of=now_local(),
    )


@dataclass
class ActionCenterView:
    groups: list[ActionGroup]      # actionable groups with >=1 open item
    heads_up: list[ActionItem]     # informational items (not in total_items)
    total_items: int
    error_count: int
    warn_count: int
    as_of: datetime

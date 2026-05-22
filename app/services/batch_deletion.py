"""Roll back an ImportBatch and the rows it created.

Each ImportFileKind has a different blast radius, so deletion is per-kind:

  TIKTOK_ORDERS      delete Order rows owned by this batch (cascades to lines).
                     Also zero out fee/refund columns? NO — those columns are
                     written by the SETTLEMENT importer, not the orders one.
                     Lines disappear via Order.lines cascade="all, delete-orphan".

  TIKTOK_SETTLEMENTS delete Settlement + Adjustment rows. The settlement importer
                     ALSO back-fills Order.{tiktok_fees, affiliate_commission,
                     shop_ads_cost, shipping_cost, refunds, sub-buckets…}. After
                     deleting the rows, recompute those Order columns from any
                     remaining Settlements for the same order (or zero if none).
                     Order.order_type promotions to SAMPLE/PAID_SAMPLE are
                     intentionally NOT reverted — they could have been
                     orders-file truth too (gross_sales == 0 heuristic).

  TIKTOK_PAYOUTS     delete Payout rows owned by this batch.

  SAMPLES            delete Sample rows owned by this batch.

  SUPPLIER_RECEIPTS  delete SampleInventoryMovement IN rows owned by this batch.
                     Shipment OUT movements don't carry import_batch_id, so the
                     import_batch_id filter scopes deletion to receipts only —
                     rolling back a receipt batch NEVER disturbs shipment history.

  SKU_MASTER         catalog-only: Sku rows have no import_batch_id, so we
                     can't tell which Sku rows this batch created vs. updated.
                     Audit-entry delete only — caller surfaces the warning.

  BUNDLE_MAPPING     same as SKU_MASTER. Bundle rows have no import_batch_id.

After per-kind cleanup, the ImportBatch row itself is deleted. All cleanup
happens in one transaction so a failure leaves the DB unchanged.
"""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ad_spend import AdSpend
from app.models.import_batch import ImportBatch, ImportFileKind
from app.models.inventory_snapshot import InventorySnapshot
from app.models.tiktok_daily_metric import TikTokDailyMetric
from app.models.order import Order
from app.models.payout import Payout
from app.models.sample import Sample
from app.models.sample_inventory_movement import SampleInventoryMovement
from app.models.settlement import Adjustment, Settlement

# Fields the settlement importer back-fills onto Order. Kept in one place so
# the recompute logic below stays in sync if the importer adds new columns.
SETTLEMENT_BACKFILL_FIELDS = (
    "refunds",
    "tiktok_fees",
    "tiktok_referral_fee",
    "tiktok_transaction_fee",
    "tiktok_refund_admin_fee",
    "tiktok_sales_tax_on_referral",
    "tiktok_smart_promo_fee",
    "tiktok_campaign_fees",
    "tiktok_partner_commission",
    "tiktok_managed_service",
    "affiliate_commission",
    "shop_ads_cost",
    "shipping_cost",
)


@dataclass
class DeletionResult:
    kind: ImportFileKind
    rows_deleted: int
    orders_recomputed: int = 0
    audit_only: bool = False  # True for catalog kinds that can't roll back data


def delete_batch(db: Session, batch: ImportBatch) -> DeletionResult:
    """Roll back `batch`. Caller is responsible for committing."""
    if batch.kind == ImportFileKind.TIKTOK_ORDERS:
        result = _delete_orders(db, batch)
    elif batch.kind == ImportFileKind.TIKTOK_SETTLEMENTS:
        result = _delete_settlements(db, batch)
    elif batch.kind == ImportFileKind.TIKTOK_PAYOUTS:
        result = _delete_payouts(db, batch)
    elif batch.kind == ImportFileKind.TIKTOK_ADS:
        result = _delete_ad_spend(db, batch)
    elif batch.kind == ImportFileKind.TIKTOK_ANALYTICS:
        result = _delete_analytics(db, batch)
    elif batch.kind == ImportFileKind.SAMPLES:
        result = _delete_samples(db, batch)
    elif batch.kind == ImportFileKind.INVENTORY_SNAPSHOT:
        result = _delete_inventory_snapshots(db, batch)
    elif batch.kind == ImportFileKind.SUPPLIER_RECEIPTS:
        result = _delete_supplier_receipts(db, batch)
    elif batch.kind in (ImportFileKind.SKU_MASTER, ImportFileKind.BUNDLE_MAPPING):
        # Catalog rows don't track batch ownership — see module docstring.
        result = DeletionResult(kind=batch.kind, rows_deleted=0, audit_only=True)
    else:
        raise ValueError(f"unknown ImportFileKind: {batch.kind}")

    db.delete(batch)
    return result


def _delete_orders(db: Session, batch: ImportBatch) -> DeletionResult:
    orders = db.execute(
        select(Order).where(Order.import_batch_id == batch.id)
    ).scalars().all()
    n = len(orders)
    for o in orders:
        db.delete(o)  # cascade="all, delete-orphan" drops lines
    return DeletionResult(kind=batch.kind, rows_deleted=n)


def _delete_settlements(db: Session, batch: ImportBatch) -> DeletionResult:
    settlements = db.execute(
        select(Settlement).where(Settlement.import_batch_id == batch.id)
    ).scalars().all()
    affected_order_ids = {s.tiktok_order_id for s in settlements if s.tiktok_order_id}

    n = len(settlements)
    for s in settlements:
        db.delete(s)

    adj_n = db.execute(
        select(Adjustment).where(Adjustment.import_batch_id == batch.id)
    ).scalars().all()
    for a in adj_n:
        db.delete(a)
    n += len(adj_n)

    db.flush()  # so the recompute query below doesn't see deleted rows

    recomputed = _recompute_orders_from_remaining_settlements(db, affected_order_ids)
    return DeletionResult(
        kind=batch.kind, rows_deleted=n, orders_recomputed=recomputed
    )


def _recompute_orders_from_remaining_settlements(
    db: Session, order_ids: set[str]
) -> int:
    """For each affected order, recompute Order.* from any REMAINING settlements.

    Matches the importer's sum-across-settlements semantics (see
    app/importers/tiktok_settlements._backfill_order). When no settlement
    remains, the fields are zeroed.
    """
    if not order_ids:
        return 0

    affected = db.execute(
        select(Order).where(Order.tiktok_order_id.in_(order_ids))
    ).scalars().all()

    count = 0
    for order in affected:
        remaining = db.execute(
            select(Settlement)
            .where(Settlement.tiktok_order_id == order.tiktok_order_id)
        ).scalars().all()

        if not remaining:
            for f in SETTLEMENT_BACKFILL_FIELDS:
                setattr(order, f, Decimal("0"))
        else:
            for f in SETTLEMENT_BACKFILL_FIELDS:
                src = _source_field(f)
                total = sum(
                    (Decimal(str(getattr(s, src) or 0)) for s in remaining),
                    Decimal("0"),
                )
                setattr(order, f, total)
        count += 1
    return count


def _source_field(order_field: str) -> str:
    """Map an Order column name to its Settlement source.

    All current fields share the same name on both models except `refunds`,
    which is sourced from Settlement.gross_sales_refund.
    """
    return "gross_sales_refund" if order_field == "refunds" else order_field


def _delete_payouts(db: Session, batch: ImportBatch) -> DeletionResult:
    payouts = db.execute(
        select(Payout).where(Payout.import_batch_id == batch.id)
    ).scalars().all()
    for p in payouts:
        db.delete(p)
    return DeletionResult(kind=batch.kind, rows_deleted=len(payouts))


def _delete_ad_spend(db: Session, batch: ImportBatch) -> DeletionResult:
    rows = db.execute(
        select(AdSpend).where(AdSpend.import_batch_id == batch.id)
    ).scalars().all()
    for r in rows:
        db.delete(r)
    return DeletionResult(kind=batch.kind, rows_deleted=len(rows))


def _delete_analytics(db: Session, batch: ImportBatch) -> DeletionResult:
    rows = db.execute(
        select(TikTokDailyMetric).where(TikTokDailyMetric.import_batch_id == batch.id)
    ).scalars().all()
    for r in rows:
        db.delete(r)
    return DeletionResult(kind=batch.kind, rows_deleted=len(rows))


def _delete_samples(db: Session, batch: ImportBatch) -> DeletionResult:
    samples = db.execute(
        select(Sample).where(Sample.import_batch_id == batch.id)
    ).scalars().all()
    for s in samples:
        db.delete(s)
    return DeletionResult(kind=batch.kind, rows_deleted=len(samples))


def _delete_supplier_receipts(db: Session, batch: ImportBatch) -> DeletionResult:
    """Delete SampleInventoryMovement rows imported by THIS batch.

    Scoped to import_batch_id == batch.id, so only IN rows the supplier-receipt
    importer wrote get deleted. Shipment OUT movements have no import_batch_id
    (see record_sample_shipment) and are therefore untouchable from this path.
    """
    rows = db.execute(
        select(SampleInventoryMovement).where(
            SampleInventoryMovement.import_batch_id == batch.id
        )
    ).scalars().all()
    for r in rows:
        db.delete(r)
    return DeletionResult(kind=batch.kind, rows_deleted=len(rows))


def _delete_inventory_snapshots(db: Session, batch: ImportBatch) -> DeletionResult:
    """Roll back snapshots imported by THIS batch.

    The importer upserts on (sku, captured_at), so re-uploading a corrected
    snapshot transfers `import_batch_id` to the latest batch. Deleting that
    latest batch therefore removes the most recent on-hand reading for those
    SKUs — the planner falls back to the prior snapshot automatically.
    """
    rows = db.execute(
        select(InventorySnapshot).where(InventorySnapshot.import_batch_id == batch.id)
    ).scalars().all()
    for r in rows:
        db.delete(r)
    return DeletionResult(kind=batch.kind, rows_deleted=len(rows))

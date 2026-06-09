"""ImportBatch tracks every file upload: what was uploaded, when, status, errors.

Every imported row (Order, Settlement, Payout, Sample) carries an `import_batch_id`
so a bad import can be rolled back by deleting one batch.
"""
import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utc_now_naive() -> datetime:
    """tz-stripped UTC `now` — DateTime columns here are naive by convention."""
    return datetime.now(UTC).replace(tzinfo=None)


class ImportFileKind(str, enum.Enum):
    TIKTOK_ORDERS = "tiktok_orders"
    TIKTOK_SETTLEMENTS = "tiktok_settlements"
    TIKTOK_PAYOUTS = "tiktok_payouts"
    TIKTOK_ADS = "tiktok_ads"
    TIKTOK_ANALYTICS = "tiktok_analytics"
    TIKTOK_GMV_MAX = "tiktok_gmv_max"
    SKU_MASTER = "sku_master"
    BUNDLE_MAPPING = "bundle_mapping"
    SAMPLES = "samples"
    INVENTORY_SNAPSHOT = "inventory_snapshot"
    SUPPLIER_RECEIPTS = "supplier_receipts"


class ImportBatchStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)
    kind: Mapped[ImportFileKind] = mapped_column(Enum(ImportFileKind), index=True)
    status: Mapped[ImportBatchStatus] = mapped_column(
        Enum(ImportBatchStatus), default=ImportBatchStatus.PENDING, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

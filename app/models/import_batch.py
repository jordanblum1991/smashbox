"""ImportBatch tracks every file upload: what was uploaded, when, status, errors.

Every imported row (Order, Settlement, Payout, Sample) carries an `import_batch_id`
so a bad import can be rolled back by deleting one batch.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ImportFileKind(str, enum.Enum):
    TIKTOK_ORDERS = "tiktok_orders"
    TIKTOK_SETTLEMENTS = "tiktok_settlements"
    TIKTOK_PAYOUTS = "tiktok_payouts"
    SKU_MASTER = "sku_master"
    BUNDLE_MAPPING = "bundle_mapping"
    SAMPLES = "samples"


class ImportBatchStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[ImportFileKind] = mapped_column(Enum(ImportFileKind), index=True)
    status: Mapped[ImportBatchStatus] = mapped_column(
        Enum(ImportBatchStatus), default=ImportBatchStatus.PENDING, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

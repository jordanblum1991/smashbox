"""Per-stream TikTok sync watermark + last-run status.

One row per stream (orders / settlements / payouts). `synced_through` is the
incremental watermark — the next sync pulls from there forward, so re-runs don't
re-fetch the whole history. `last_status` / `last_message` drive the connection
status panel and (later) failure alerts.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class TikTokSyncState(Base):
    __tablename__ = "tiktok_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # orders|settlements|payouts
    synced_through: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # watermark
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(16), default="never", nullable=False)  # never|ok|empty|pending|error
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    rows_last_run: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False
    )

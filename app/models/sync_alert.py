"""Per-condition state for the sync-failure email alerter.

One row per stable alert `key` (e.g. "tiktok:settlements", "gmv_max"). The
edge-triggered alerter (app/services/sync_alerts.py) emails on the ok→alerting
and alerting→ok transitions; this row remembers the current state so a persisting
failure doesn't re-spam and a recovery emails exactly once.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class SyncAlert(Base):
    __tablename__ = "sync_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)  # ok|alerting
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_transition_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)

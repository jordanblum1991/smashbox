"""Shop = tenant boundary for the multi-brand rollout (Phase 2).

Today there's exactly one shop ("smashbox", created by the boot migration).
Phase 2b will start filtering every report query by `current_user.shop_id`;
Phase 2c will add a super-admin shop-switcher UI.

`timezone` is captured on the shop because TikTok Seller Center buckets
days in the shop's local time — see the daily reconciliation investigation
in CLAUDE.md. Once Phase 2b ships, daily reports can render in this TZ to
match TikTok's display exactly.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64), default="America/Los_Angeles")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)

"""Stored TikTok Marketing API authorization — advertiser ad-spend access.

SEPARATE from TikTokCredential (the Shop API). The Marketing API
(business-api.tiktok.com) issues a single long-lived access token per
authorization — no refresh token, no expiry to track — scoped to one or more
advertiser accounts. Captured by `/auth/tiktok-ads/callback`.

One row (single connected app). The token is a secret — never logged.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class TikTokMarketingCredential(Base):
    __tablename__ = "tiktok_marketing_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)  # long-lived
    advertiser_id: Mapped[str | None] = mapped_column(String(64), nullable=True)    # primary
    advertiser_ids: Mapped[str | None] = mapped_column(Text, nullable=True)         # comma-joined
    advertiser_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    granted_scopes: Mapped[str | None] = mapped_column(Text, nullable=True)

    connected_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False
    )

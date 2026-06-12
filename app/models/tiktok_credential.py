"""Stored TikTok Shop API authorization — access/refresh tokens + shop cipher.

One row per connected shop (today: one, Smashbox). Captured by the authorize
callback (`/auth/tiktok/callback`) and rotated by the refresh job. The access
token is short-lived; `refresh_token` mints a new one before `access_expires_at`.
`shop_cipher` is required on every data API call.

Tokens are secrets — never logged or serialized into a response.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class TikTokCredential(Base):
    __tablename__ = "tiktok_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[str | None] = mapped_column(String(128), nullable=True)        # TikTok shop id
    shop_cipher: Mapped[str | None] = mapped_column(String(255), nullable=True)    # required on data calls
    shop_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seller_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    region: Mapped[str | None] = mapped_column(String(16), nullable=True)

    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    granted_scopes: Mapped[str | None] = mapped_column(Text, nullable=True)        # comma-joined

    connected_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False
    )

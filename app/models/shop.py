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

    # ---- SAP inventory auto-sync schedule (user-editable on the Uploads page) -
    # The in-process scheduler reads these to register a cron job that pulls the
    # SAP feed. `days` is an APScheduler day_of_week string ("mon,tue,...");
    # hour/minute are in this shop's `timezone`. Editing them on the Uploads page
    # live-reschedules the running job. Defaults seed weekday 07:30 local.
    inventory_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    inventory_sync_hour: Mapped[int] = mapped_column(Integer, default=7)
    inventory_sync_minute: Mapped[int] = mapped_column(Integer, default=30)
    inventory_sync_days: Mapped[str] = mapped_column(
        String(64), default="mon,tue,wed,thu,fri"
    )

    # ---- TikTok Marketing (GMV-Max) auto-sync schedule (admin-managed on
    # /admin/tiktok-ads). Same scheduling shape as the SAP sync above; decoupled
    # from the inventory job so the ad data can refresh on its own cadence.
    gmv_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    gmv_sync_hour: Mapped[int] = mapped_column(Integer, default=7)
    gmv_sync_minute: Mapped[int] = mapped_column(Integer, default=45)
    gmv_sync_days: Mapped[str] = mapped_column(
        String(64), default="mon,tue,wed,thu,fri,sat,sun"
    )

    # ---- Weekly inventory-report email (admin-managed on /reports/inventory) --
    # Same scheduling shape as the SAP sync above. Recipients is a comma-
    # separated list; the report emails to all of them. Off + no recipients by
    # default so nothing sends until configured.
    inventory_report_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    inventory_report_hour: Mapped[int] = mapped_column(Integer, default=8)
    inventory_report_minute: Mapped[int] = mapped_column(Integer, default=0)
    inventory_report_days: Mapped[str] = mapped_column(String(64), default="mon")
    inventory_report_recipients: Mapped[str] = mapped_column(String(1024), default="")

    # ---- Sales-report email (managed on /reports/sales) ----------------------
    sales_report_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sales_report_hour: Mapped[int] = mapped_column(Integer, default=8)
    sales_report_minute: Mapped[int] = mapped_column(Integer, default=0)
    sales_report_days: Mapped[str] = mapped_column(String(64), default="mon")
    sales_report_recipients: Mapped[str] = mapped_column(String(1024), default="")
    sales_report_period: Mapped[str] = mapped_column(String(32), default="prev_month")

    # ---- Sample-report email (managed on /reports/samples) -------------------
    sample_report_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sample_report_hour: Mapped[int] = mapped_column(Integer, default=8)
    sample_report_minute: Mapped[int] = mapped_column(Integer, default=0)
    sample_report_days: Mapped[str] = mapped_column(String(64), default="mon")
    sample_report_recipients: Mapped[str] = mapped_column(String(1024), default="")
    sample_report_period: Mapped[str] = mapped_column(String(32), default="prev_month")

    @property
    def report_recipients_list(self) -> list[str]:
        """Recipient emails, parsed + trimmed from the comma-separated column."""
        return [a.strip() for a in (self.inventory_report_recipients or "").split(",")
                if a.strip()]

    @property
    def sales_report_recipients_list(self) -> list[str]:
        return [a.strip() for a in (self.sales_report_recipients or "").split(",") if a.strip()]

    @property
    def sample_report_recipients_list(self) -> list[str]:
        return [a.strip() for a in (self.sample_report_recipients or "").split(",") if a.strip()]

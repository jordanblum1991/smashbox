"""Legacy GMV Max campaign metrics — per (year, month) campaign-attributed
figures. Superseded by GmvMaxDailyMetric (daily granularity); no active code
path writes here, but historical rows exist (see baseline/baseline_report.md,
which totals gross_revenue). Modeled so the table is managed by migrations and
its data is preserved on the Postgres move. Schema matches the existing table
exactly (indexes on year/month/shop_id, UNIQUE(year, month))."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class GmvMaxCampaignMetric(Base):
    __tablename__ = "gmv_max_campaign_metrics"
    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_gmv_max_campaign_metrics_year_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)
    gross_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    sku_orders: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive
    )

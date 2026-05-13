from decimal import Decimal

from sqlalchemy import Boolean, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Sku(Base):
    """SKU master. COGS is per unit and used by the P&L engine."""
    __tablename__ = "skus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(512))
    brand: Mapped[str] = mapped_column(String(64), index=True)
    unit_cogs: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    list_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

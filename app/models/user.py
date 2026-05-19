"""Per-user accounts for the dashboard. Replaces the v1 shared HTTP Basic
credential. Phase 1 of the multi-user roadmap: one tenant, real per-user
identity. Phase 2 will add per-shop scoping by introducing a Shop FK on this
table and on every transactional table.

Password storage: bcrypt via passlib (`passlib[bcrypt]`). Never log or
serialize `password_hash` in any response.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class UserRole(str, enum.Enum):
    """Roles are forward-looking — admin is needed for the eventual user
    management UI and (later) cross-shop access. Today every authenticated
    user can use every report; role enforcement kicks in when we wire up
    admin-only routes."""
    ADMIN = "admin"
    MEMBER = "member"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), default=UserRole.MEMBER, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

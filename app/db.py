from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

def _connect_args() -> dict:
    url = settings.database_url
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    if url.startswith("postgresql"):
        # Fly Managed Postgres' `attach` points us at pgbouncer in TRANSACTION
        # pooling mode. psycopg3 auto-prepares a statement after a few uses;
        # under transaction pooling the prepared statement lives on one pooled
        # server connection and won't exist on the next transaction's
        # connection ("prepared statement does not exist"). Disable server-side
        # prepared statements to stay pooler-safe.
        return {"prepare_threshold": None}
    return {}


engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    # pgbouncer/network can silently drop idle connections; validate on checkout
    # so a stale connection becomes a transparent reconnect, not a 500.
    pool_pre_ping=not settings.database_url.startswith("sqlite"),
    connect_args=_connect_args(),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

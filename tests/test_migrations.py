"""Guard: the Alembic migrations stay in sync with the ORM models.

A fresh `alembic upgrade head` must reproduce exactly the tables + columns that
Base.metadata defines. If someone adds/changes a model without a migration,
this fails — catching the drift before it reaches a Postgres deploy.
"""
import app.models  # noqa: F401  — registers every model on Base.metadata
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.config import settings
from app.db import Base


def test_alembic_head_matches_models(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    # env.py drives the connection from settings.database_url — point it here.
    monkeypatch.setattr(settings, "database_url", url)

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")

    insp = inspect(create_engine(url))
    migrated = set(insp.get_table_names()) - {"alembic_version"}
    model_tables = set(Base.metadata.tables)

    assert migrated == model_tables, (
        "tables drifted: "
        f"only-in-migration={migrated - model_tables} "
        f"only-in-models={model_tables - migrated}"
    )

    # Column-level parity per table.
    for t in sorted(model_tables):
        mig_cols = {c["name"] for c in insp.get_columns(t)}
        model_cols = set(Base.metadata.tables[t].columns.keys())
        assert mig_cols == model_cols, f"{t} columns drifted: {model_cols ^ mig_cols}"

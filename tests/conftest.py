"""Shared pytest configuration.

Critical: tests MUST run against an isolated database. Several tests call
`Base.metadata.drop_all()` / `create_all()` to start with a clean slate — if
that hits the dev SQLite at `data/smashbox.db`, your imported catalog and
orders get wiped. We redirect the engine to a per-session temp file BEFORE
any test imports models, so the dev DB stays intact regardless of how
aggressive a test is.
"""
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Point the app at a temp SQLite BEFORE any `from app.db import ...` happens.
# `Settings` reads DATABASE_URL at module-import time, so this must run early.
_TMP_DB = Path(tempfile.gettempdir()) / "smashbox_tests.sqlite"
if _TMP_DB.exists():
    _TMP_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

# Pin auth OFF for tests, hermetically. Many tests assume gated routes return
# 200 directly (no /login redirect), which holds only when session_secret == "".
# A developer's local .env may set SESSION_SECRET (for local login preview); an
# explicit env var beats the .env file in pydantic-settings, so force it empty
# here BEFORE app.config builds Settings. Without this, the whole suite goes red
# the moment someone adds SESSION_SECRET to their .env.
os.environ["SESSION_SECRET"] = ""

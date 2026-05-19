from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # File-system locations. In production these point at the Fly persistent
    # volume mount (`/data`, `/data/uploads`, `/data/exports`); locally they
    # default to the project directory. Override via env vars on deploy.
    data_dir: Path = REPO_ROOT / "data"
    upload_dir: Path = REPO_ROOT / "uploads"
    export_dir: Path = REPO_ROOT / "exports"

    # SQLite file lives inside data_dir. Set DATABASE_URL explicitly to point
    # at Postgres later; otherwise we derive it from data_dir at startup.
    database_url: str | None = None

    default_brand: str = "smashbox"

    # ---- Auth (per-user sessions) -----------------------------------------
    # Phase 1: real users instead of the v1 shared HTTP Basic credential.
    #
    # session_secret signs the session cookie (Starlette SessionMiddleware via
    # itsdangerous). Empty string = auth disabled — convenient for local dev
    # so reloads don't sign you out. Production MUST set this to a long random
    # string via `fly secrets set SESSION_SECRET=...`.
    session_secret: str = ""

    # First-time bootstrap. When no User rows exist and these are populated,
    # the startup hook creates an admin user with these credentials. Set
    # before the first deploy, then unset (or leave alone — they're ignored
    # once a user exists).
    initial_admin_email: str = ""
    initial_admin_password: str = ""
    initial_admin_name: str = "Admin"

    # ---- Auth (legacy, kept for backward-compat with old deploys) ---------
    # Phase 1's session middleware is the new gate. These remain so a fresh
    # deploy that hasn't yet set SESSION_SECRET stays protected by Basic Auth
    # rather than wide-open. Empty values keep the legacy path inert.
    basic_auth_username: str = "smashbox"
    basic_auth_password: str = ""

    # ---- Business-rule caps -----------------------------------------------
    # Cap on Outlandish-funded portion of a seller-funded discount, as a
    # fraction of the order's eligible base. Smashbox absorbs anything over.
    # See app/rules/seller_funded_split.py.
    outlandish_cap_pct: Decimal = Decimal("0.10")

    # Policy: total seller-funded discount should NEVER exceed this fraction of
    # the eligible base. Anything over is imported (Smashbox still absorbs it
    # so the exact-sum invariant holds) but flagged as a policy violation.
    seller_funded_policy_cap_pct: Decimal = Decimal("0.30")


settings = Settings()

# Resolve the default DB URL once the data_dir is finalized.
if settings.database_url is None:
    settings.database_url = f"sqlite:///{settings.data_dir / 'smashbox.db'}"

# Ensure directories exist at every startup (including on a fresh Fly volume).
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.upload_dir.mkdir(parents=True, exist_ok=True)
settings.export_dir.mkdir(parents=True, exist_ok=True)

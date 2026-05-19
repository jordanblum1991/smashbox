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

    # ---- Auth (HTTP Basic) ------------------------------------------------
    # Single shared credential gating the whole app. Username defaults to
    # "smashbox"; password MUST be set via env var when deployed (`fly secrets
    # set BASIC_AUTH_PASSWORD=...`). Empty password disables auth entirely —
    # used for local dev so we don't have to log in on every reload.
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

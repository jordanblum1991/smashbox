from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{REPO_ROOT / 'data' / 'smashbox.db'}"
    default_brand: str = "smashbox"

    # Cap on Outlandish-funded portion of a seller-funded discount, as a
    # fraction of the order's eligible base. Smashbox absorbs anything over.
    # See app/rules/seller_funded_split.py.
    outlandish_cap_pct: Decimal = Decimal("0.10")

    # Policy: total seller-funded discount should NEVER exceed this fraction of
    # the eligible base. Anything over is imported (Smashbox still absorbs it
    # so the exact-sum invariant holds) but flagged as a policy violation.
    seller_funded_policy_cap_pct: Decimal = Decimal("0.30")

    # Free monthly sample allowance (units). Over this counts as paid oversampling.
    free_sample_monthly_allowance: int = 100

    upload_dir: Path = REPO_ROOT / "uploads"
    export_dir: Path = REPO_ROOT / "exports"


settings = Settings()
settings.upload_dir.mkdir(parents=True, exist_ok=True)
settings.export_dir.mkdir(parents=True, exist_ok=True)

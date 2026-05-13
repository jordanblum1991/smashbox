from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{REPO_ROOT / 'data' / 'smashbox.db'}"
    default_brand: str = "smashbox"

    # Outlandish share of seller-funded discounts (0.0 - 1.0).
    # Smashbox share = 1 - this. The two MUST add back exactly to the total.
    seller_funded_outlandish_share: Decimal = Decimal("0.5")

    # Free monthly sample allowance (units). Over this counts as paid oversampling.
    free_sample_monthly_allowance: int = 100

    upload_dir: Path = REPO_ROOT / "uploads"
    export_dir: Path = REPO_ROOT / "exports"


settings = Settings()
settings.upload_dir.mkdir(parents=True, exist_ok=True)
settings.export_dir.mkdir(parents=True, exist_ok=True)

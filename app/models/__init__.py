"""Importing this module registers all ORM models with SQLAlchemy's metadata."""
from app.models.ad_credit import AdCredit
from app.models.ad_spend import AdSpend
from app.models.bundle import Bundle, BundleComponent
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.models.payout import Payout
from app.models.sample import Sample
from app.models.settlement import Adjustment, Settlement
from app.models.sku import Sku

__all__ = [
    "AdCredit",
    "AdSpend",
    "Adjustment",
    "Bundle",
    "BundleComponent",
    "ImportBatch",
    "ImportBatchStatus",
    "ImportFileKind",
    "Order",
    "OrderLine",
    "OrderType",
    "Payout",
    "Sample",
    "Settlement",
    "Sku",
]


def register_models() -> None:
    """No-op anchor so main.py can `import register_models` for the side-effect."""
    return None

"""Importing this module registers all ORM models with SQLAlchemy's metadata."""
from app.models.ad_credit import AdCredit
from app.models.ad_spend import AdSpend
from app.models.bundle import Bundle, BundleComponent
from app.models.creator import Creator
from app.models.gmv_max_campaign_metric import GmvMaxCampaignMetric
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.gmv_max_reimbursement import GmvMaxReimbursement
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.inventory_snapshot import InventorySnapshot
from app.models.invoice import Invoice
from app.models.order import Order, OrderLine, OrderType
from app.models.payout import Payout
from app.models.purchase_invoice import (
    PurchaseInvoice,
    PurchaseInvoiceCredit,
    PurchaseInvoicePayment,
)
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.sample import Sample
from app.models.sample_allowance import SampleAllowance
from app.models.sample_inventory_movement import SampleInventoryMovement, SampleMovementType
from app.models.sample_inventory_snapshot import SampleInventorySnapshot
from app.models.tiktok_credential import TikTokCredential
from app.models.settlement import Adjustment, Settlement
from app.models.shop import Shop
from app.models.sku import Sku
from app.models.sku_alias import SkuAlias
from app.models.tiktok_daily_metric import TikTokDailyMetric
from app.models.user import User, UserRole

__all__ = [
    "AdCredit",
    "AdSpend",
    "Adjustment",
    "Bundle",
    "BundleComponent",
    "Creator",
    "GmvMaxCampaignMetric",
    "GmvMaxDailyMetric",
    "GmvMaxReimbursement",
    "ImportBatch",
    "ImportBatchStatus",
    "ImportFileKind",
    "InventorySnapshot",
    "Invoice",
    "Order",
    "OrderLine",
    "OrderType",
    "Payout",
    "PurchaseInvoice",
    "PurchaseInvoiceCredit",
    "PurchaseInvoicePayment",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "Sample",
    "SampleAllowance",
    "SampleInventoryMovement",
    "SampleInventorySnapshot",
    "SampleMovementType",
    "Settlement",
    "Shop",
    "Sku",
    "TikTokCredential",
    "SkuAlias",
    "TikTokDailyMetric",
    "User",
    "UserRole",
]


def register_models() -> None:
    """No-op anchor so main.py can `import register_models` for the side-effect."""
    return None

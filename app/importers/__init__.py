"""File importers, one per ImportFileKind.

Each importer subclasses BaseImporter and is registered in IMPORTERS below so
the upload route can dispatch by kind.
"""
from app.importers.base import BaseImporter, ImportResult
from app.importers.bundle_mapping import BundleMappingImporter
from app.importers.sku_master import SkuMasterImporter
from app.importers.tiktok_orders import TikTokOrdersImporter
from app.importers.tiktok_settlements import TikTokSettlementsImporter
from app.models.import_batch import ImportFileKind

IMPORTERS: dict[ImportFileKind, type[BaseImporter]] = {
    ImportFileKind.TIKTOK_ORDERS: TikTokOrdersImporter,
    ImportFileKind.TIKTOK_SETTLEMENTS: TikTokSettlementsImporter,
    ImportFileKind.SKU_MASTER: SkuMasterImporter,
    ImportFileKind.BUNDLE_MAPPING: BundleMappingImporter,
    # TODO: TIKTOK_PAYOUTS, SAMPLES
}

__all__ = ["BaseImporter", "ImportResult", "IMPORTERS"]

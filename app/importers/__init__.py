"""File importers, one per ImportFileKind.

Each importer subclasses BaseImporter and is registered in IMPORTERS below so
the upload route can dispatch by kind.
"""
from app.importers.base import BaseImporter, ImportResult
from app.importers.bundle_mapping import BundleMappingImporter
from app.importers.samples import SamplesImporter
from app.importers.sku_master import SkuMasterImporter
from app.importers.tiktok_ads import TikTokAdsImporter
from app.importers.tiktok_orders import TikTokOrdersImporter
from app.importers.tiktok_payouts import TikTokPayoutsImporter
from app.importers.tiktok_settlements import TikTokSettlementsImporter
from app.models.import_batch import ImportFileKind

IMPORTERS: dict[ImportFileKind, type[BaseImporter]] = {
    ImportFileKind.TIKTOK_ORDERS: TikTokOrdersImporter,
    ImportFileKind.TIKTOK_SETTLEMENTS: TikTokSettlementsImporter,
    ImportFileKind.TIKTOK_PAYOUTS: TikTokPayoutsImporter,
    ImportFileKind.TIKTOK_ADS: TikTokAdsImporter,
    ImportFileKind.SKU_MASTER: SkuMasterImporter,
    ImportFileKind.BUNDLE_MAPPING: BundleMappingImporter,
    ImportFileKind.SAMPLES: SamplesImporter,
}

__all__ = ["BaseImporter", "ImportResult", "IMPORTERS"]

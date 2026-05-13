"""File importers, one per ImportFileKind.

Each importer subclasses BaseImporter and is registered in IMPORTERS below so
the upload route can dispatch by kind.
"""
from app.importers.base import BaseImporter, ImportResult
from app.importers.tiktok_orders import TikTokOrdersImporter
from app.importers.tiktok_settlements import TikTokSettlementsImporter
from app.models.import_batch import ImportFileKind

IMPORTERS: dict[ImportFileKind, type[BaseImporter]] = {
    ImportFileKind.TIKTOK_ORDERS: TikTokOrdersImporter,
    ImportFileKind.TIKTOK_SETTLEMENTS: TikTokSettlementsImporter,
    # TODO: TIKTOK_PAYOUTS, SKU_MASTER, BUNDLE_MAPPING, SAMPLES
}

__all__ = ["BaseImporter", "ImportResult", "IMPORTERS"]

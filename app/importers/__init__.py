"""File importers, one per ImportFileKind.

Each importer subclasses BaseImporter and is registered in IMPORTERS below so
the upload route can dispatch by kind.
"""
from app.importers.base import BaseImporter, ImportResult
from app.importers.tiktok_orders import TikTokOrdersImporter
from app.models.import_batch import ImportFileKind

IMPORTERS: dict[ImportFileKind, type[BaseImporter]] = {
    ImportFileKind.TIKTOK_ORDERS: TikTokOrdersImporter,
    # TODO: TIKTOK_SETTLEMENTS, TIKTOK_PAYOUTS, SKU_MASTER, BUNDLE_MAPPING, SAMPLES
}

__all__ = ["BaseImporter", "ImportResult", "IMPORTERS"]

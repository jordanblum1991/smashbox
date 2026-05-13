"""BaseImporter — every file kind implements this contract.

Importers are stateless transformers: file in, rows persisted, ImportResult out.
They never silently drop rows; anything skipped goes into ImportResult.errors with
enough detail to fix the source file.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.import_batch import ImportBatch


@dataclass
class ImportResult:
    rows_imported: int = 0
    rows_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.rows_skipped += 1
        self.errors.append(reason)


class BaseImporter(ABC):
    """Subclasses parse one file kind and persist rows tied to `batch`."""

    @abstractmethod
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        """Read `path`, write rows to `db`, return a result.

        Must NOT commit — the caller commits once the batch is fully processed
        so a parse failure rolls back the whole file.
        """

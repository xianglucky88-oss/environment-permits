from __future__ import annotations
from .extract import parse_permit
from .models import PermitDocument
from .sources import DocumentSource
from .storage import PermitStore


class PermitPipeline:
    def __init__(self, source: DocumentSource, store: PermitStore) -> None:
        self.source = source
        self.store = store

    def collect_one(self, reference: str) -> PermitDocument:
        document = parse_permit(reference, self.source.read(reference))
        self.store.upsert(document)
        return document

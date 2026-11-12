from __future__ import annotations
import sqlite3
from pathlib import Path
from .models import PermitDocument


class PermitStore:
    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(database)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS permits (
            document_id TEXT PRIMARY KEY, company_name TEXT NOT NULL, permit_number TEXT NOT NULL,
            document_type TEXT NOT NULL, issued_at TEXT NOT NULL, valid_until TEXT,
            pollutants TEXT NOT NULL, source_url TEXT NOT NULL, raw_text TEXT NOT NULL,
            collected_at TEXT NOT NULL
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS attachments (
            document_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
            PRIMARY KEY (document_id, url)
        )""")
        self.connection.commit()

    def upsert(self, document: PermitDocument) -> None:
        self.connection.execute("""INSERT OR REPLACE INTO permits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            document.document_id, document.company_name, document.permit_number,
            document.document_type, document.issued_at, document.valid_until,
            ",".join(document.pollutants), document.source_url, document.raw_text,
            document.collected_at,
        ))
        self.connection.executemany("INSERT OR REPLACE INTO attachments VALUES (?, ?, ?)", [
            (document.document_id, item.url, item.title) for item in document.attachments
        ])
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
